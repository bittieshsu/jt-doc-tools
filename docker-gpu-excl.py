"""列出 uv.lock 裡的 GPU / NVIDIA 專有套件，輸出成 uv 的排除參數。

給 `Dockerfile` 用。**從 lock 現算而不是寫死**：lock 更新換了套件名
（`nvidia-cudnn-cu13` 這種帶版本後綴的很常換），寫死的清單會悄悄失效，
症狀是「映像檔突然大了 2 GB」，沒有人會注意到。

為什麼要排除：Linux 的 torch wheel 會拉進 NVIDIA CUDA 執行期，那是**專有
授權**元件；本程式是 AGPL-3.0，打包成同一個散布物有疑慮。而且 **Docker 分層
不可變** —— 「先裝再刪」對映像檔大小與授權都沒有意義，只能根本不裝。
"""
import re
import sys

lock = open("uv.lock", encoding="utf-8").read()
names = sorted(set(re.findall(r'^name = "(nvidia-[^"]+|triton|cuda-[^"]+)"',
                              lock, re.M)))
sys.stdout.write(" ".join(f"--no-install-package {n}" for n in names))
