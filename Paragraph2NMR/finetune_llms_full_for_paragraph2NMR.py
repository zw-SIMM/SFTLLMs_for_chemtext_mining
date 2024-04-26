import os
# Setup environment (full fine-tuning 7b needs 160GB Memory)
os.environ["CUDA_VISIBLE_DEVICES"] = "4,5,6,7"

import torch
import pandas as pd
from datasets import load_dataset, Dataset, DatasetDict
from transformers import  AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, HfArgumentParser, TrainingArguments
import logging
from peft import LoraConfig, PeftModel
from trl import SFTTrainer
from transformers import TrainerCallback

# Data Loading and Preprocessing
train_file = "data/data_for_llms/train/train_200_data_in_300.csv"
test_file = "data/data_for_llms/test/test_300.csv"
train_df = pd.read_csv(train_file, encoding='utf-8')
test_df = pd.read_csv(test_file, encoding='utf-8')

def create_assistant_message(row):
    return f"""{{\"IUPAC\":\"{row['IUPAC']}\",\"1H NMR text\":\"{row['1H NMR text']}\",\"1H NMR conditions\":\"{row['1H NMR conditions']}\",\"1H NMR data\":\"{row['1H NMR data']}\",\"13C NMR text\":\"{row['13C NMR text']}\",\"13C NMR conditions\":\"{row['13C NMR conditions']}\",\"13C NMR data\":\"{row['13C NMR data']}\"}}"""

train_df['NMRInfo'] = train_df.apply(create_assistant_message, axis=1)
test_df['NMRInfo'] = test_df.apply(create_assistant_message, axis=1)

source_text = "Paragraph"
target_text = "NMRInfo"
instruction = f'{source_text}2{target_text}: '
instruction = '''Extract the NMR information from the Paragraph: '''
# instruction = '''Extract text containing 1H NMR and 13C NMR data, remove interference information such as reactants, raw materials, solvents and other non-final product names based on text semantics, and then extract the name, code or number of the final product. Please delete the IUPAC name Alias, numbers and ordinal numbers before and after fields, such as '2.1.3.', '(HL4)', '(9)', '(4d)'. NMR text should contain complete information, such as instrument power and solvent information, For example, "13C NMR text": "13C NMR (400 MHz, acetone-d6) 174.0 (C), 157.7 (C). Then split the NMR text. The content in NMR conditions is NMR instrument power and solvent information, such as "13C NMR conditions": "400MHz, acetone-d6". The contents in the NMR data are only numbers, such as "13C NMR data": "174.0, 157.7", "1H NMR data": "174.0, 157.7". All responses must originate from information extracted from the given text, ensuring that the extracted content has not been modified or fragmented, and that capitalization and punctuation are exactly the same as the given text. Must end with {"IUPAC":"text","1H NMR text":"text","1H NMR conditions":"text","1H NMR data":"text","13C NMR text":"text","13C NMR conditions":"text","13C NMR data":"text"} format reply.'''

# Old prompt template (for Mistral)
train_df['text'] = f'<s>[INST] {instruction}' + train_df[source_text] + " [/INST] " + train_df[target_text] +  "!!! </s>"
test_df['text'] = f'<s>[INST] {instruction}' + test_df[source_text] + " [/INST] " + test_df[target_text] +  "!!! </s>"

train_dataset = Dataset.from_dict(train_df[['text']].astype(str))
test_dataset = Dataset.from_dict(test_df[['text']].astype(str))
dataset = DatasetDict({"train": train_dataset, "test": test_dataset})
print(dataset)

# TrainingArguments parameters
num_train_epochs = 20
save_steps = 0                      # Save checkpoint every X updates steps
logging_steps = 25                  # Log every X updates steps

fp16 = False                        # Enable fp16/bf16 training (set fp16 to True with an V100)
bf16 = True                         # Enable fp16/bf16 training (set bf16 to True with an A100)

per_device_train_batch_size = 2     # Batch size per GPU for training
per_device_eval_batch_size = 2      # Batch size per GPU for evaluation
gradient_accumulation_steps = 1     # Number of update steps to accumulate the gradients for
gradient_checkpointing = True       # Enable gradient checkpointing

max_grad_norm = 0.3                 # Maximum gradient normal (gradient clipping)
learning_rate = 5e-6                # Initial learning rate (AdamW optimizer, 1e-5 or 5e-6 or 1e-4)
weight_decay = 0.001                # Weight decay to apply to all layers except bias/LayerNorm weights

optim = "paged_adamw_32bit"         # Optimizer to use
lr_scheduler_type = "cosine"        # Learning rate schedule

max_steps = -1                      # Number of training steps (overrides num_train_epochs)
warmup_ratio = 0.03                 # Ratio of steps for a linear warmup (from 0 to learning rate)

group_by_length = True              # Group sequences into batches with same length (Saves memory and speeds up training considerably)

# SFT parameters
max_seq_length = 4096               # Maximum sequence length to use (default 1024)
packing = False                     # Pack multiple short examples in the same input sequence to increase efficiency
device_map = "auto"                 # Load the entire model on the GPU 0, or "auto"

# Model Version (Meta-Llama-3-8B-Instruct, Mistral-7B-Instruct-v0.2, llama-2-13b-chat-hf ...)
model_name = "/home/zhangwei/pretrained_models/Mistral-7B-Instruct-v0.2"  # Path of the pretrained model downloaded from Hugging Face
new_model_dir = f"saved_models/Mistral-7B-Instruct-v0.2train_{len(train_df)}_without_prompt_lr{learning_rate}_bs{per_device_train_batch_size}"  # Fine-tuned model name
output_dir = new_model_dir          # Output directory where the model predictions and checkpoints will be stored

# Load base model
model = AutoModelForCausalLM.from_pretrained(
    pretrained_model_name_or_path = model_name,
    torch_dtype = torch.bfloat16,
    device_map = device_map
)

# Load tokenizer
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
tokenizer.pad_token = tokenizer.eos_token
tokenizer.padding_side = "right"  # Fix weird overflow issue with fp16 training
print("------------tokenizer.eos_token--------------", tokenizer.eos_token)
print("------------tokenizer.unk_token--------------", tokenizer.unk_token)
print("------------tokenizer.bos_token--------------", tokenizer.bos_token)
print("------------tokenizer.pad_token--------------", tokenizer.pad_token)
print("------------vocab_size is------------", tokenizer.vocab_size)
print("------------vocab_size is------------", len(tokenizer))

# Set training parameters
training_arguments = TrainingArguments(
    output_dir = output_dir,
    logging_dir = output_dir + "/logs/",
    # evaluation_strategy = "epoch",            
    save_strategy = "epoch",
    num_train_epochs = num_train_epochs,
    save_total_limit = num_train_epochs,
    per_device_train_batch_size = per_device_train_batch_size,
    gradient_accumulation_steps = gradient_accumulation_steps,
    # optim = optim,
    #save_steps=save_steps,
    logging_steps = logging_steps,
    learning_rate = learning_rate,
    weight_decay = weight_decay,
    fp16 = fp16,
    bf16 = bf16,
    max_grad_norm = max_grad_norm,
    max_steps = max_steps,
    warmup_ratio = warmup_ratio,
    group_by_length = group_by_length,
    lr_scheduler_type = lr_scheduler_type,
    report_to = "tensorboard",
)

# Set supervised fine-tuning parameters
trainer = SFTTrainer(
    model = model,
    train_dataset = dataset['train'],
    eval_dataset = dataset["test"],
    dataset_text_field = "text",
    max_seq_length = max_seq_length,
    tokenizer = tokenizer,
    args = training_arguments,
    packing = packing,
)

class SaveBestModelCallback(TrainerCallback):
    def __init__(self):
        super().__init__()
        self.best_eval_loss = float('inf')
        self.best_model_checkpoint = None
        
    def on_log(self, args, state, control, logs=None, **kwargs):
        # Check if training_loss is in the logs and print it
        if 'loss' in logs:
            training_loss = logs['loss']
            logging.info(f"Epoch: {int(state.epoch)}, Step: {state.global_step}, Current training_loss: {training_loss}")

    # def on_evaluate(self, args, state, control, **kwargs):
    #     # Check if eval_loss is in the logs
    #     if 'eval_loss' in state.log_history[-1]:
    #         eval_loss = state.log_history[-1]['eval_loss']
    #         # Print current eval_loss with epoch and step
    #         logging.info(f"Epoch: {int(state.epoch)}, Step: {state.global_step}, Current eval_loss: {eval_loss}")  

    #         if eval_loss < self.best_eval_loss:
    #             self.best_eval_loss = eval_loss
    #             # Save the best model
    #             self.best_model_checkpoint = state.global_step
    #             trainer.save_model(f"{args.output_dir}/best_model")
    #             # Print loss of Best Model
    #             logging.info(f"New best model saved at step {state.global_step} with eval_loss: {eval_loss}")  

# Create an instance of the callback
save_best_model_callback = SaveBestModelCallback()

# Training and logging
logging.basicConfig(filename=output_dir+'/training.log', level=logging.INFO)
logging.info(f"""[Device]: cuda:{os.environ["CUDA_VISIBLE_DEVICES"]}...\n""")
logging.info(f"""[Model]: Loading {model_name}...\n""")
logging.info(f"""[Outputdir]: Loading {output_dir}...\n""")

# Add the callback to the trainer
trainer.add_callback(save_best_model_callback)

# Train model
trainer.train()

# Save trained model
trainer.model.save_pretrained(new_model_dir)