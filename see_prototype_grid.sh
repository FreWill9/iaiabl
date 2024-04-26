#!/bin/bash

source /Users/frederikwillger/my_env/bin/activate

nvidia-smi

srun -u python3 vis_protos.py
