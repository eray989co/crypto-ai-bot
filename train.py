import os, json, torch, torch.nn as nn, numpy as np, datetime, pytz, sys, pandas as pd
from torch.utils.data import TensorDataset, DataLoader
from sklearn.metrics import accuracy_score, f1_score
from data.utils import SYMBOLS, get_kline_by_strategy, compute_features, create_dataset
from model.base_model import get_model
from model_weight_loader import get_model_weight
from feature_importance import compute_feature_importance, save_feature_importance
from wrong_data_loader import load_training_prediction_data
from failure_db import load_existing_failure_hashes
from logger import log_training_result, strategy_stats, load_failure_count
from window_optimizer import find_best_window
import hashlib
from collections import Counter
import sqlite3

DEVICE = torch.device("cpu")
MODEL_DIR = "./persistent/models"
os.makedirs(MODEL_DIR, exist_ok=True)
now_kst = lambda: datetime.datetime.now(pytz.timezone("Asia/Seoul"))
NUM_CLASSES = 16
STRATEGY_WRONG_REP = {"단기": 4, "중기": 6, "장기": 8}

def get_feature_hash_from_tensor(x):
    if x.ndim != 2 or x.shape[0] == 0:
        return "invalid"
    last = x[-1].tolist()
    rounded = [round(float(val), 2) for val in last]
    return hashlib.sha1(",".join(map(str, rounded)).encode()).hexdigest()

def get_frequent_failures(min_count=5):
    counter = Counter()
    try:
        with sqlite3.connect("/persistent/logs/failure_patterns.db") as conn:
            rows = conn.execute("SELECT hash FROM failure_patterns").fetchall()
            for row in rows:
                counter[row[0]] += 1
    except:
        return set()
    return {h for h, cnt in counter.items() if cnt >= min_count}

def save_model_metadata(symbol, strategy, model_type, acc, f1, loss):
    meta = {
        "symbol": symbol,
        "strategy": strategy,
        "model": model_type,
        "accuracy": float(round(acc, 4)),
        "f1_score": float(round(f1, 4)),
        "loss": float(round(loss, 6)),
        "timestamp": now_kst().strftime("%Y-%m-%d %H:%M:%S")
    }
    path = f"{MODEL_DIR}/{symbol}_{strategy}_{model_type}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"🗘 저장됨: {path}"); sys.stdout.flush()

def train_one_model(symbol, strategy, max_epochs=20):
    print(f"▶ 학습 시작: {symbol}-{strategy}")
    try:
        df = get_kline_by_strategy(symbol, strategy)
        if df is None or df.empty:
            print("⏭ 데이터 없음"); return

        df_feat = compute_features(symbol, df, strategy)
        if df_feat is None or len(df_feat) < 30:
            print("⏭ 피처 부족"); return

        if "timestamp" not in df_feat.columns:
            df_feat["timestamp"] = df_feat.get("datetime", pd.Timestamp.now())
        df_feat = df_feat.dropna()

        features = df_feat.to_dict(orient="records")
        window = find_best_window(symbol, strategy)
        if not isinstance(window, int) or window <= 0:
            print(f"[스킵] {symbol}-{strategy} → find_best_window 실패 또는 무효값")
            return

        X_raw, y_raw = create_dataset(features, window=window, strategy=strategy)
        if X_raw is None or y_raw is None or len(X_raw) == 0:
            print(f"[스킵] {symbol}-{strategy} → create_dataset 결과 없음")
            return

        # ✅ 유효성 검사 및 정제
        X_filtered, y_filtered = [], []
        for xi, yi in zip(X_raw, y_raw):
            if not isinstance(xi, np.ndarray) or xi.ndim != 2: continue
            if not isinstance(yi, (int, np.integer)) or not (0 <= yi < NUM_CLASSES): continue
            X_filtered.append(xi)
            y_filtered.append(yi)

        if len(X_filtered) < 5:
            print("⏭ 학습용 시퀀스 부족"); return

        X_raw = np.array(X_filtered)
        y_raw = np.array(y_filtered)
        input_size = X_raw.shape[2]

        val_len = int(len(X_raw) * 0.2)
        if val_len == 0:
            print("⏭ 검증 데이터 부족"); return

        X_train, X_val = X_raw[:-val_len], X_raw[-val_len:]
        y_train, y_val = y_raw[:-val_len], y_raw[-val_len:]

        # ✅ 실패 피처 해시 준비
        failure_hashes = load_existing_failure_hashes()
        frequent_failures = get_frequent_failures(min_count=5)

        # ✅ 실패 데이터 로드
        wrong_data = load_training_prediction_data(symbol, strategy, input_size, window, source_type="wrong")
        wrong_filtered = []
        used_hashes = set()

        for s in wrong_data:
            if isinstance(s, (list, tuple)) and len(s) >= 2:
                xb, yb = s[:2]
                if not isinstance(xb, np.ndarray) or xb.shape != (window, input_size): continue
                if not isinstance(yb, (int, np.integer)) or not (0 <= yb < NUM_CLASSES): continue
                feature_hash = get_feature_hash_from_tensor(torch.tensor(xb))
                if feature_hash in used_hashes or feature_hash in failure_hashes or feature_hash in frequent_failures:
                    continue
                used_hashes.add(feature_hash)
                wrong_filtered.append((xb, yb))

        for model_type in ["lstm", "cnn_lstm", "transformer"]:
            model = get_model(model_type, input_size).train()
            model_path = f"{MODEL_DIR}/{symbol}_{strategy}_{model_type}.pt"
            if os.path.exists(model_path):
                try:
                    model.load_state_dict(torch.load(model_path, map_location=DEVICE))
                    print(f"🔁 이어 학습: {model_path}")
                except:
                    print(f"[로드 실패] {model_path} → 새로 학습")

            optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
            lossfn = nn.CrossEntropyLoss()

            # ✅ 실패 데이터 학습 (배치 학습 방식 적용)
            if wrong_filtered:
                print(f"[실패 학습 시작] 총 {len(wrong_filtered)} 샘플")
                wrong_X = torch.tensor([x for x, _ in wrong_filtered], dtype=torch.float32)
                wrong_y = torch.tensor([y for _, y in wrong_filtered], dtype=torch.long)
                wrong_ds = TensorDataset(wrong_X, wrong_y)
                wrong_loader = DataLoader(wrong_ds, batch_size=16, shuffle=True)

                for _ in range(6):  # 반복 횟수는 조정 가능
                    for xb, yb in wrong_loader:
                        model.train()
                        logits = model(xb)
                        loss = lossfn(logits, yb)
                        if not torch.isfinite(loss): continue
                        optimizer.zero_grad(); loss.backward(); optimizer.step()

            # ✅ 일반 데이터 학습
            train_ds = TensorDataset(torch.tensor(X_train, dtype=torch.float32),
                                     torch.tensor(y_train, dtype=torch.long))
            train_loader = DataLoader(train_ds, batch_size=32, shuffle=True)
            for _ in range(max_epochs):
                model.train()
                for xb, yb in train_loader:
                    logits = model(xb)
                    loss = lossfn(logits, yb)
                    if not torch.isfinite(loss): break
                    optimizer.zero_grad(); loss.backward(); optimizer.step()

            # ✅ 검증 및 저장
            model.eval()
            with torch.no_grad():
                xb = torch.tensor(X_val, dtype=torch.float32)
                yb = torch.tensor(y_val, dtype=torch.long)
                logits = model(xb)
                preds = torch.argmax(logits, dim=1).numpy()
                acc = accuracy_score(y_val, preds)
                f1 = f1_score(y_val, preds, average="macro")
                val_loss = lossfn(logits, yb).item()

            if acc >= 1.0 and len(set(y_val)) <= 2:
                print(f"⚠️ 오버핏 감지 → 저장 중단")
                log_training_result(symbol, strategy, f"오버핏({model_type})", acc, f1, val_loss)
                continue

            torch.save(model.state_dict(), model_path)
            save_model_metadata(symbol, strategy, model_type, acc, f1, val_loss)
            log_training_result(symbol, strategy, model_type, acc, f1, val_loss)

            try:
                imps = compute_feature_importance(model, xb, yb, list(df_feat.drop(columns=["timestamp"]).columns))
                save_feature_importance(imps, symbol, strategy, model_type)
            except:
                print("⚠️ 중요도 저장 실패 (무시됨)")

    except Exception as e:
        print(f"[오류] {symbol}-{strategy} → {e}")
        try:
            log_training_result(symbol, strategy, f"실패({str(e)})", 0.0, 0.0, 0.0)
        except:
            print("⚠️ 로그 기록 실패")

def train_all_models():
    for strat in ["단기", "중기", "장기"]:
        for sym in SYMBOLS:
            try: train_one_model(sym, strat)
            except Exception as e:
                print(f"[전체 학습 오류] {sym}-{strat} → {e}")
