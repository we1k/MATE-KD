
import argparse
import json
import logging
import math
import os
import random
from pathlib import Path

import datasets
import evaluate
import torch
import torch.nn as nn
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from datasets import load_dataset
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    SchedulerType,
)

# from src.model import CILDA
from src.Args import parse_args
from src import CILDA

logger = get_logger(__name__)

def main():

    # setting for logger
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    datasets.utils.logging.set_verbosity_warning()
    transformers.utils.logging.set_verbosity_info()
    args = parse_args()
    cil_model = CILDA(args)
    # cil_model.train_teacher(train_epochs=args.teacher_num_train_epochs)
    # cil_model.eval_on_clf(cil_model.Teacher)
    # cil_model.train_generator(train_epochs=args.generator_num_train_epochs) 
    cil_model.generate_synthetic_data()
    # cil_model.train_student(train_epochs=args.student_num_train_epochs)
    
if __name__ == '__main__':
    main()