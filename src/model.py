import csv
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
import evaluate


from datasets import load_dataset
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    AutoConfig,
    AutoModelForMaskedLM,
    DataCollatorForLanguageModeling,
    AutoModelForSequenceClassification,
    GPT2LMHeadModel,
    GPT2Tokenizer,
    CTRLLMHeadModel,
    CTRLTokenizer,
    AutoTokenizer,
    DataCollatorWithPadding,
    PretrainedConfig,
    default_data_collator,
)

from src.dataset import GLUE_Dataset

logger = get_logger(__name__)

class CILDA:
    def __init__(self, args):
        self.task_num_labels = {
            "sst2": 2,
            "cola": 2,
            "mnli": 2,
            "qnli": 2,
        }

        self.args = args
        if torch.cuda.is_available():
            self.device = torch.device('cuda')
        else:
            self.device = torch.device('cpu')
            
        # TODO: change MASK BERT to GPT2
        # self.Generator = GPT2LMHeadModel.from_pretrained(args.generator_model_name)
        # from transformers import RobertaForSequenceClassification

        # self.G_tokenizer = GPT2Tokenizer.from_pretrained(args.generator_model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(args.teacher_model_name)
        if args.generator_checkpoint_path is not None:
            self.Generator = AutoModelForMaskedLM.from_pretrained(args.generator_checkpoint_path).to(self.device)
        else:
            self.Generator = AutoModelForMaskedLM.from_pretrained(args.generator_model_name).to(self.device)
            
        self.num_labels = self.task_num_labels[args.task_name]

        self.S_config = AutoConfig.from_pretrained(args.student_model_name, num_labels=self.num_labels)
        self.Student = AutoModelForSequenceClassification.from_pretrained(args.student_model_name, config=self.S_config).to(self.device)

        if args.teacher_checkpoint_path is not None:
            logger.info(f"loading teacher weight from {args.teacher_checkpoint_path}") 
            self.Teacher = AutoModelForSequenceClassification.from_pretrained(args.teacher_checkpoint_path).to(self.device) 
        else:
            self.T_config = AutoConfig.from_pretrained(args.teacher_model_name, num_labels=self.num_labels)
            self.Teacher = AutoModelForSequenceClassification.from_pretrained(args.teacher_model_name, config=self.T_config).to(self.device)

        
        # self.t_proj = nn.Linear(self.Teacher.config.intermediate_size, args.intermediate_hidden_size)
        # self.s_proj = nn.Linear(self.Student.config.intermediate_size, args.intermediate_hidden_size)

        self.select_k_per_class = args.select_k_per_class
        
        self.data_dict = self.get_data_dict(args.task_name)
            
    
    def get_data_dict(self, task):
        data_dict = {}
        dataset = GLUE_Dataset(self.args, self.tokenizer)
        
        k = self.select_k_per_class
        k_val = 500
        print('k = ', k, '  k-val = ',k_val)
        
        few_dataloader = dataset.get_final_ds(task=task, batch_size=self.args.batch_size, split='train', k=k)
        full_dataloader = dataset.get_final_ds(task=task, batch_size=self.args.batch_size, split='train', k=-1)
        eval_dataloader = dataset.get_final_ds(task=task, batch_size=self.args.batch_size, split='validation', k=k_val)
        test_dataloader = dataset.get_final_ds(task=task, batch_size=self.args.batch_size, split='test', k=-1)
        
        data_dict['few-shot'] = few_dataloader
        data_dict['full'] = full_dataloader
        data_dict['eval'] = eval_dataloader
        data_dict['test'] = test_dataloader

        return data_dict
    

    def get_optimizer(self, model_name):
        args = self.args
        no_decay = ["bias", "LayerNorm.weight"]
        if model_name == 'generator':
            model = self.Generator
            lr = self.args.generator_lr
        elif model_name == 'teacher':
            model = self.Teacher
            lr = self.args.teacher_lr
        elif model_name == 'student':
            model = self.Student
            lr = self.args.student_lr
        else:
            raise KeyError(f"Error key model_name {model_name}") 
        
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.args.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=lr)
        return optimizer

    def train_teacher(self, train_epochs):
        args = self.args
        model = self.Teacher
        model.train()

        optimizer = self.get_optimizer(model_name='teacher')
       
        metric = evaluate.load('glue', self.args.task_name)
        
        train_dataloader = self.data_dict['full']
        eval_dataloader = self.data_dict['eval']

        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        
        max_train_steps = args.max_train_steps
        if args.max_train_steps is None:
            max_train_steps = train_epochs * num_update_steps_per_epoch

        train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch) 
        total_batch_size = args.per_device_train_batch_size * args.gradient_accumulation_steps

        logger.info("***** Running Teacher training *****")
        logger.info(f"  Num Epochs = {train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_train_steps}")

        progress_bar = tqdm(range(max_train_steps))
        for epoch in range(train_epochs):
            model.train()
            if args.with_tracking:
                total_loss = 0
            active_dataloader = train_dataloader
            for step, batch in enumerate(active_dataloader):
                batch = {k : batch[k].to(self.device) for k in batch}
                outputs = model(
                    input_ids=batch['input_ids'],
                    attention_mask=batch['attention_mask'],
                    labels=batch['clf_labels']
                    )
                loss = outputs.loss
                # We keep track of the loss at each epoch
                if args.with_tracking:
                    total_loss += loss.detach().float()
                loss = loss / args.gradient_accumulation_steps
                loss.backward()

                if step % 3*args.gradient_accumulation_steps == 0 or step == len(active_dataloader) - 1:
                    optimizer.step()
                    optimizer.zero_grad()
                    progress_bar.update(1)

                # evaluation
                if step % args.eval_every_step == 0:
                    eval_metric = self.eval_on_clf(model)
                    logger.info(f'epoch {epoch}: {eval_metric}')

        save_path = os.path.join(self.args.output_dir, 'few-shot')
        print(save_path)
        model.save_pretrained(save_path)


    def eval_on_clf(self, model):
        model.eval()
        metric = evaluate.load('glue', self.args.task_name)
        eval_dataloader = self.data_dict['eval']

        for _, batch in enumerate(eval_dataloader):
            with torch.no_grad():
                batch = {k : batch[k].to(self.device) for k in batch}
                outputs = model(
                    input_ids=batch['input_ids'],
                    attention_mask=batch['attention_mask'],
                    labels=batch['clf_labels'] 
                )
            predictions = outputs.logits.argmax(dim=-1)
            references  = batch['clf_labels']
            metric.add_batch(
                predictions=predictions,
                references=references,
            )
        eval_metric = metric.compute()
        return eval_metric

    def train_generator(self, train_epochs):
        generator = self.Generator
        teacher = self.Teacher
        student = self.Student
        teacher.eval()
        student.eval()
        
        # adversarial training
        args = self.args
        g_optimizer = self.get_optimizer('generator')
        
        metric = evaluate.load('glue', self.args.task_name)
        train_dataloader = self.data_dict['few-shot']
        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        
        max_train_steps = args.max_train_steps
        if args.max_train_steps is None:
            max_train_steps = train_epochs * num_update_steps_per_epoch
        
        train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch) 
        total_batch_size = args.per_device_train_batch_size * args.gradient_accumulation_steps
        
        logger.info("***** Running ADV training *****")
        logger.info(f"  Num Epochs = {train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_train_steps}")
        logger.info(f"  num_update_steps_per_epoch = {num_update_steps_per_epoch}")
        
        for epoch in range(train_epochs):
            for i, batch in enumerate(tqdm(train_dataloader)):
                # train generator
                logger.info("training the genrator")
                generator.train()
                student.eval()
                batch = {k : batch[k].to(self.device) for k in batch}
                batch_size = batch['input_ids'].shape[0]
                seq_len = batch['input_ids'].shape[1]
                outputs = generator(
                    input_ids=batch['input_ids'],
                    attention_mask=batch['attention_mask'],
                )
                logits = outputs.logits
                # mask.shape -> (batch, seq_len, embed_dim)
                mask = F.gumbel_softmax(logits, tau=1, hard=True, dim=-1)
                idx = torch.arange(0, self.tokenizer.vocab_size, dtype=torch.float32, device=self.device, requires_grad=True).reshape(1, 1, -1).repeat(batch_size, seq_len, 1)
                
                # synthetic data.shape - > (batch, seq)
                synthetic_data = torch.sum(mask * idx , dim=2).long() * batch['attention_mask']
                batch['synthetic_input_ids'] = batch['input_ids'].clone()
                # only change the mask_idx 
                # puts label back to input_ids

                for i in range(batch_size):
                    mask_idx = (batch['lm_labels'][i] != -100).nonzero().squeeze()
                    batch['input_ids'][i][mask_idx] = batch['lm_labels'][i][mask_idx]
                    batch['synthetic_input_ids'][i][mask_idx] = synthetic_data[i][mask_idx]
                    logger.info(f"syn_text:{self.tokenizer.decode(batch['synthetic_input_ids'][i],skip_special_tokens=True)}")
                    logger.info(f"ori_text:{self.tokenizer.decode(batch['input_ids'][i],skip_special_tokens=True)}")
                    logger.info("*"*10)
                
                # compare and KL loss
                # logits.shape -> (batch, num_labels)
                teacher_logits = teacher(
                    input_ids=batch['synthetic_input_ids'],
                    # attention_mask=batch['attention_mask'],
                ).logits
                student_logits = student(
                    input_ids=batch['synthetic_input_ids'],
                    # attention_mask=batch['attention_mask'],
                ).logits
                
                loss = -torch.nn.KLDivLoss()(teacher_logits, student_logits)
                
                loss.backward()
                g_optimizer.step()
                g_optimizer.zero_grad()

        # test on this
        generator_save_path = os.path.join(self.args.output_dir, 'generator')
        generator.save_pretrained(generator_save_path)
    
    def generate_synthetic_data(self, syn_data_output_path):
        generator = self.Generator
        generator.eval()

        all_synthetic_data = []
        all_real_data = []
        real_label = []
        with torch.no_grad():
            for i, batch in enumerate(tqdm(self.data_dict['few-shot'])):
                batch = {k : batch[k].to(self.device) for k in batch}
                batch_size = batch['input_ids'].shape[0]
                seq_len = batch['input_ids'].shape[1]
                outputs = generator(
                    input_ids=batch['input_ids'],
                    attention_mask=batch['attention_mask'],
                )
                logits = outputs.logits
                # mask.shape -> (batch, seq_len, embed_dim)
                mask = F.gumbel_softmax(logits, tau=1, hard=True, dim=-1)
                idx = torch.arange(0, self.tokenizer.vocab_size, dtype=torch.float32, requires_grad=True, device=self.device).reshape(1, 1, -1).repeat(batch_size, seq_len, 1)

                
                # synthetic data.shape - > (batch, seq)
                synthetic_data = torch.sum(mask * idx , dim=2).long() * batch['attention_mask']
                batch['synthetic_input_ids'] = batch['input_ids'].clone()
                # only change the mask_idx 
                # puts label back to input_ids
                for i in range(batch_size):
                    mask_idx = (batch['lm_labels'][i] != -100).nonzero().squeeze()
                    batch['input_ids'][i][mask_idx] = batch['lm_labels'][i][mask_idx]
                    batch['synthetic_input_ids'][i][mask_idx] = synthetic_data[i][mask_idx]
                    logger.info(f"syn_text:{self.tokenizer.decode(batch['synthetic_input_ids'][i],skip_special_tokens=True)}")
                    logger.info(f"ori_text:{self.tokenizer.decode(batch['input_ids'][i],skip_special_tokens=True)}")
                    logger.info("*"*10)
                
                
                all_real_data.append(batch['input_ids']) 
                all_synthetic_data.append(batch['synthetic_input_ids'])
                real_label.append(batch['clf_labels']) 
        
        # combine all data
        all_synthetic_data = torch.cat(all_synthetic_data, dim=0).to(self.device)
        all_real_data = torch.cat(all_real_data, dim=0).to(self.device)
        real_label = torch.cat(real_label, dim=0).to(self.device)
        
        real_text      = self.tokenizer.batch_decode(all_real_data, skip_special_tokens=True)
        synthetic_text = self.tokenizer.batch_decode(all_synthetic_data, skip_special_tokens=True)
        real_label = real_label.detach().cpu().numpy().tolist()
        augmented_dataset = [
                {"ori_text":text, "syn_text": syn_text, "label":label} \
                for text, syn_text, label in zip(real_text, synthetic_text, real_label)
            ]
        
        # write constructed data into json file
        with open(syn_data_output_path, 'w') as jsonfile:
            json.dump(augmented_dataset, jsonfile)
    
    def get_synthetic_dataset(self):
        args = self.args
        tokenizer = self.tokenizer
        max_length = args.max_length
        
        file_path = os.path.join('data', args.syn_data_path)
        raw_datasets = load_dataset('json',data_files=file_path)

        def preproces_fn(examples):
            inputs = tokenizer(examples['ori_text'], padding='max_length', max_length=max_length, truncation=True)
            syn_input = tokenizer(examples['syn_text'], padding='max_length', max_length=max_length, truncation=True).input_ids

            inputs['syn_input_ids'] = syn_input
            inputs['labels'] = examples['label']
            return inputs

        process_ds = raw_datasets.map(preproces_fn, batched=True)
        process_ds.remove_columns(column_names=raw_datasets['train'].column_names)
        process_ds.set_format('torch', columns=['input_ids', 'attention_mask', 'labels', 'syn_input_ids'])

        data_collator = DataCollatorWithPadding(tokenizer, padding=True,pad_to_multiple_of=8)
        dataloader = DataLoader(process_ds['train'], collate_fn=data_collator, batch_size=args.batch_size)
        self.data_dict['synthetic'] = dataloader
        return 
                
    def train_student(self, train_epochs):
        args = self.args
        
        generator = self.Generator
        teacher = self.Teacher
        student = self.Student
        teacher.eval()
        generator.eval()
        
        s_optimizer = self.get_optimizer('student')
        
        if 'synthetic' not in self.data_dict:
            self.get_synthetic_dataset()
            
        train_dataloader = self.data_dict['synthetic']
        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        
        max_train_steps = args.max_train_steps
        if args.max_train_steps is None:
            max_train_steps = train_epochs * num_update_steps_per_epoch
        
        train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch) 
        total_batch_size = args.per_device_train_batch_size * args.gradient_accumulation_steps
        
        # train a student
        for epoch in range(train_epochs):
            logger.info("training on student")
            student.train()
            for i, batch in enumerate(tqdm(train_dataloader)): 
                batch = {k : batch[k].to(self.device) for k in batch}
                
                # few-shot dataset
                real_teacher_logits = teacher(
                    input_ids=batch['input_ids'],
                ).logits
                real_student_output = student(
                    input_ids=batch['input_ids'],
                    labels=batch['labels'],
                )
                
                # syn_data training
                syn_teacher_logits = teacher(
                    input_ids=batch['syn_input_ids'],
                ).logits
                syn_student_logits = student(
                    input_ids=batch['syn_input_ids'],
                ).logits
                
                real_student_logits = real_student_output.logits
                
                real_t_pred = F.log_softmax(real_teacher_logits, dim=1)
                real_s_pred = F.log_softmax(real_student_logits, dim=1)
                
                syn_t_pred = F.log_softmax(syn_teacher_logits, dim=1)
                syn_s_pred = F.log_softmax(syn_student_logits, dim=1)
                
                loss = F.kl_div(real_s_pred, real_t_pred, reduction='batchmean', log_target=True) + F.kl_div(syn_s_pred, syn_t_pred, reduction='batchmean', log_target=True)
                # print(F.kl_div(real_s_pred, real_t_pred, reduction='batchmean', log_target=True).item(), 
                #      F.kl_div(syn_s_pred, syn_t_pred, reduction='batchmean', log_target=True).item(),
                #      real_student_output.loss.item()) 
                loss = (loss + real_student_output.loss) / 3
                logger.info(f"loss : {loss.item()}")
                loss.backward()
                s_optimizer.step()
                s_optimizer.zero_grad()

            # eval on student
            if epoch % 10 == 0:
                student.eval()
                eval_metric = self.eval_on_clf(student)
                logger.info(f'test results: {eval_metric}')