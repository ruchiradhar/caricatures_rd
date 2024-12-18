from tqdm.auto import tqdm
import os
import argparse
import shutil
import copy

from accelerate import Accelerator
from accelerate.utils import set_seed

import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F

from transformers import (
    AutoTokenizer, default_data_collator,
    AutoModelForCausalLM,
    AutoConfig, GenerationConfig,
    get_scheduler,
)

from datasets import load_dataset

import scan_constants
from rl_trainers import PPOConfig, PPOTrainer

from trl import AutoModelForCausalLMWithValueHead


def train(args, accelerator):
    
    # dataset
    raw_datasets = load_dataset(args.dataset, args.dataset_config, trust_remote_code=True)

    # split train set into train and validation
    train_val_split = raw_datasets['train'].train_test_split(test_size=args.validation_split, seed=args.seed)
    raw_datasets['train'] = train_val_split['train']
    raw_datasets['validation'] = train_val_split['test']

    column_names = raw_datasets["train"].column_names
    input_column = column_names[0]
    output_column = column_names[1]

    # format dataset with dummy tokens
    scan_constants.special_tokens_dict["additional_special_tokens"] = [scan_constants.dummy_token]

    def add_empty_token(x):
        command_str = x[input_column]
        command = command_str.split()
        padded_command = []
        index = 0
        c = 0
        while index < scan_constants.command_max_len:
            expected_cs = scan_constants.command_structure[index]
            if c < len(command) and command[c] in expected_cs:
                padded_command.append(command[c])
                c += 1
            else:
                padded_command.append(scan_constants.dummy_token)
            index += 1
        
        x[input_column] = ' '.join(padded_command)
        return x

    with accelerator.main_process_first():
        raw_datasets["train"] = raw_datasets["train"].map(
            add_empty_token,
            batched=False,
            num_proc=args.num_workers, 
            desc="Running tokenizer on dataset",
        )
        raw_datasets["validation"] = raw_datasets["validation"].map(
            add_empty_token,
            batched=False,
            num_proc=args.num_workers,
            desc="Running tokenizer on dataset",
    )
        

    # model and tokenizer
    config = AutoConfig.from_pretrained(args.model_checkpoint, trust_remote_code=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model_checkpoint, use_fast=True, trust_remote_code=True)
    tokenizer.add_special_tokens(scan_constants.special_tokens_dict)

    model = AutoModelForCausalLM.from_pretrained(
                args.model_checkpoint,
                config=config,
                trust_remote_code=True,
            )
    # Resize the embeddings only when necessary to avoid index errors
    embedding_size = model.get_input_embeddings().weight.shape[0]
    if len(tokenizer) > embedding_size:
        model.resize_token_embeddings(len(tokenizer))
    # generation config
    generation_config = GenerationConfig.from_pretrained(args.model_checkpoint)
    generation_config.pad_token_id = tokenizer.pad_token_id
    gen_kwargs = {
        "max_new_tokens": args.max_gen_length,
        "num_beams": args.num_beams
    }

    # save and reload model for AutoModelForCausalLMWithValueHead in case token embedding was resized
    model.save_pretrained('tmp_model')
    model = AutoModelForCausalLMWithValueHead.from_pretrained('tmp_model', config=config)
    shutil.rmtree('tmp_model')

    # reference model for ppo
    ref_model = copy.deepcopy(model)

    # preprocess dataset
    def preprocess_function(examples):
        # commands, actions
        inputs = examples[input_column]
        targets = examples[output_column]

        # tokenize as single sequence separated by special token
        # left padding for batch generation
        tokenizer.padding_side = "left"
        model_inputs = tokenizer(
            [i+tokenizer.sep_token for i in inputs],
            padding='max_length', max_length=args.max_input_length
        )
        # right padding for logits
        tokenizer.padding_side = "right"
        # labels = context + actions
        model_inputs['labels'] = tokenizer(
            [i+tokenizer.sep_token for i in inputs],
            [t+tokenizer.eos_token for t in targets],
            padding='max_length', max_length=args.max_input_length
        )['input_ids']
        # eval labels
        tokenizer.padding_side = "left"
        model_inputs['eval_labels'] = tokenizer(
            [t+tokenizer.eos_token for t in targets],
            padding='max_length', max_length=args.max_input_length
        )['input_ids']

        return model_inputs

    with accelerator.main_process_first():
        train_dataset = raw_datasets["train"].map(
            preprocess_function,
            batched=True,
            num_proc=args.num_workers,
            remove_columns=column_names,
            desc="Running tokenizer on dataset",
        )
        eval_dataset = raw_datasets["validation"].map(
            preprocess_function,
            batched=True,
            num_proc=args.num_workers,
            remove_columns=column_names,
            desc="Running tokenizer on dataset",
        )


    # dataloaders
    # drop_last=True to make sure each PPO loop has same number of samples
    train_dataloader = DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=default_data_collator,
        batch_size=args.batch_size,
        drop_last=True, # do not remove
    )
    eval_dataloader = DataLoader(
        eval_dataset,
        shuffle=True,
        collate_fn=default_data_collator,
        batch_size=args.batch_size,
        drop_last=True,
    )

    # prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.lr)

    # scheduler
    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.warmup_steps * accelerator.num_processes,
        num_training_steps=args.train_steps * accelerator.num_processes,
    )


    # prepare
    model, optimizer, ref_model, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, ref_model, train_dataloader, eval_dataloader, lr_scheduler
    )

    # ppo trainer
    ppo_config = PPOConfig(
        batch_size=args.batch_size,
        mini_batch_size=args.mini_batch_size,
        max_input_length=args.max_input_length,
        ppo_epochs=args.ppo_epochs,
        generation_config=generation_config,
        gen_kwargs=gen_kwargs,
    )
    ppo_trainer = PPOTrainer(
        config=ppo_config,
        model=model,
        ref_model=ref_model,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        tokenizer=tokenizer,
        accelerator=accelerator,
    )

    # train
    num_m_batches = args.batch_size//args.mini_batch_size
    global_step = 0  # tracks total steps
    global_bar = tqdm(range(global_step, args.train_steps), disable=not accelerator.is_main_process, position=0)
    #eval_bar = tqdm(range(len(eval_dataloader)), position=1)

    while True:
        # batches are left padded
        for batch in train_dataloader:
            train_stats = ppo_trainer.step(
                batch,
                low_mem=True,
                whiten_adv=True,
                reward_kl_penalty=True,
            )
            accelerator.print('pg loss: {}, vf_loss: {}'.format(train_stats['loss/policy'], train_stats['loss/value']))
            global_bar.update(1)

            # eval
            if (global_step + 1) % args.eval_steps == 0:
                accuracy = 0
                ppo_trainer.model.eval()

                for batch in eval_dataloader:
                    mb_accuracy = 0
                    for m in range(num_m_batches):
                        mini_batch = {
                            k: v[m*args.mini_batch_size:(m+1)*args.mini_batch_size] for k, v in batch.items()
                        }
                        with torch.no_grad():
                            output_ids = accelerator.unwrap_model(ppo_trainer.model).generate(
                                input_ids = mini_batch['input_ids'],
                                attention_mask = mini_batch['attention_mask'],
                                generation_config=generation_config,
                                **gen_kwargs
                            )

                        # pad_acrss_processes to get equal length for each processs
                        output_ids = accelerator.pad_across_processes(output_ids, dim=1, pad_index=tokenizer.pad_token_id)
                        label_ids = accelerator.pad_across_processes(mini_batch["eval_labels"], dim=1, pad_index=tokenizer.pad_token_id)
                        # gather
                        output_ids = accelerator.gather(output_ids) 
                        label_ids = accelerator.gather(label_ids)  
                        # decode
                        batch_output = tokenizer.batch_decode(output_ids, skip_special_tokens=True)
                        batch_input = tokenizer.batch_decode(mini_batch['input_ids'], skip_special_tokens=True)
                        outputs = [batch_output[b].replace(batch_input[b], '') for b in range(len(batch_output))]
                        labels = tokenizer.batch_decode(label_ids, skip_special_tokens=True)
                        # compute accuracy
                        acc = [o==l for o, l in zip(outputs, labels)]
                        mb_accuracy += sum(acc)/len(acc)

                    accuracy += mb_accuracy/num_m_batches

                    #eval_bar.update(1)

                accelerator.print('accuracy: {}'.format(accuracy/len(eval_dataloader)))

            global_step += 1
            if global_step == args.train_steps:
                return



def run():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--seed",
        default=42,
        type=int,
    )
    parser.add_argument(
        "--model_checkpoint",
        default=None,
        type=str,
        help="Path to partially or fully trained model",
    )
    parser.add_argument(
        "--dataset",
        default="scan",
        type=str,
        help="Dataset",
    )
    # 'simple', 'addprim_jump', 'addprim_turn_left', 'filler_num0', 
    # 'filler_num1', 'filler_num2', 'filler_num3', 'length', 
    # 'template_around_right', 'template_jump_around_right', 
    # 'template_opposite_right', 'template_right'
    parser.add_argument(
        "--dataset_config",
        default="simple",
        type=str,
    )
    parser.add_argument(
        "--validation_split",
        type=float,
        default=0.1,
        help="The percentage of the train set used as validation set in case there's no validation split",
    )
    parser.add_argument(
        '--max_input_length',
        type=int,
        default=512
    )
    parser.add_argument(
        '--max_gen_length',
        type=int,
        default=256
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        help="The output directory where the model checkpoints and predictions will be written.",
    )
    parser.add_argument(
        '--num_workers',
        type=int,
        default=os.cpu_count(),  # 1, None, 32
        help="The number of processes to use for the preprocessing."
    )
    parser.add_argument(
        "--batch_size",
        default=512,
        type=int,
    )
    parser.add_argument(
        "--mini_batch_size",
        default=8,
        type=int,
    )
    parser.add_argument(
        "--train_steps",
        default=10000,
        type=int,
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        default=1,
        type=int,
    )
    parser.add_argument(
        "--eval_steps",
        default=100,
        type=int,
    )
    parser.add_argument(
        "--warmup_steps",
        default=0,
        type=int,
    )
    parser.add_argument(
        "--ppo_epochs",
        default=2,
        type=int,
    )
    parser.add_argument(
        "--lr",
        default=1.41e-5,
        type=float,
        help="ppo learning rate"
    )
    parser.add_argument(
        "--weight_decay",
        default=0.0,
        type=float,
        help="Weight decay if we apply some."
    )
    parser.add_argument(
        "--lr_scheduler_type",
        default='linear',
        type=str,
    )
    parser.add_argument(
        "--mixed_precision", # choose from no, fp16, bf16 or fp8
        default='no',
        type=str,
    )
    parser.add_argument(
        '--num_beams',
        type=int,
        default=1
    )

    # parse args
    args = parser.parse_args()

    # set seed
    set_seed(args.seed)

    if args.batch_size % args.mini_batch_size != 0:
        raise ValueError('batch size must be divisible by mini_batch_size') 

    if not os.path.isdir(args.output_dir):
        os.makedirs(args.output_dir)
    print('output directory set to : {}'.format(args.output_dir))

    # initialize accelerator
    accelerator = Accelerator(
        mixed_precision=args.mixed_precision,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        log_with="wandb",
        project_dir=args.output_dir
    )
    # we need to initialize the trackers we use, and also store our configuration
    track_config = {
        "lr": args.lr,
        "train_steps": args.train_steps,
        "seed": args.seed,
        "batch_size": args.batch_size,
        "mini_batch_size": args.mini_batch_size,
        "max_input_length": args.max_input_length,
        "max_gen_length": args.max_gen_length,
    }
    # run = os.path.split(__file__)[-1].split(".")[0]
    accelerator.init_trackers('runs', track_config)

    # train function
    train(args, accelerator)

    # end logging
    accelerator.end_training()


if __name__ == "__main__":

    run()