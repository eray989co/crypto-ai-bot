import torch
import torch.nn as nn

class LSTMModel(nn.Module):
    def __init__(self, input_size=4, hidden_size=50, num_layers=1, output_size=1):
        super(LSTMModel, self).__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        self.fc = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        h_0 = torch.zeros(1, x.size(0), 50)
        c_0 = torch.zeros(1, x.size(0), 50)
        _, (hn, _) = self.lstm(x, (h_0, c_0))
        out = self.fc(hn[-1])
        return out
