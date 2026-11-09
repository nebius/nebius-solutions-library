#!/usr/bin/env python3
"""
Fine-tune Llama 7B with WizardLM_evol_instruct_70k using PEFT/LoRA.
Supports single-GPU and multi-node distributed training.
"""

import argparse
import inspect
import os
import sys
from typing import Optional

import torch
from datasets import load_dataset
from peft import LoraConfig, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainerCallback,
    TrainingArguments,
)
from trl import SFTTrainer

try:
    import mlflow
except ImportError:
    mlflow = None


def is_distributed():
    return int(os.environ.get("WORLD_SIZE", 1)) > 1


def is_main_process():
    return int(os.environ.get("RANK", 0)) == 0


def is_mlflow_enabled() -> bool:
    required = [
        "MLFLOW_TRACKING_URI",
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
    ]
    return all(os.getenv(var) for var in required) and mlflow is not None


def resolve_process_device_index(local_rank: int) -> int:
    """Pick a valid CUDA index for this process under current visibility."""
    visible_count = torch.cuda.device_count()
    if visible_count <= 0:
        return 0
    return local_rank if local_rank < visible_count else 0


class MLflowMetricsCallback(TrainerCallback):
    def on_log(self, args, state, control, logs=None, **kwargs):
        if logs is None or mlflow is None:
            return
        metrics = {}
        for key, value in logs.items():
            if isinstance(value, (int, float)):
                metrics[key] = float(value)
        if metrics:
            mlflow.log_metrics(metrics, step=state.global_step)


def format_instruction(example):
    if example.get("output", "").strip():
        return (
            f"### Instruction:\n{example['instruction']}\n\n"
            f"### Response:\n{example['output']}"
        )
    return f"### Instruction:\n{example['instruction']}\n\n### Response:\n"


def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune Llama with WizardLM dataset"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="meta-llama/Llama-2-7b-hf",
        help="Base model name from Hugging Face",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./llama-wizardlm-finetune",
        help="Output directory for checkpoints",
    )
    parser.add_argument(
        "--num-epochs", type=int, default=1, help="Number of training epochs"
    )
    parser.add_argument(
        "--batch-size", type=int, default=1, help="Training batch size per device"
    )
    parser.add_argument(
        "--gradient-accum-steps",
        type=int,
        default=8,
        help="Gradient accumulation steps",
    )
    parser.add_argument(
        "--learning-rate", type=float, default=2e-4, help="Learning rate"
    )
    parser.add_argument(
        "--max-seq-length", type=int, default=512, help="Maximum sequence length"
    )
    parser.add_argument(
        "--logging-steps",
        type=int,
        default=10,
        help="How often (in optimizer steps) to emit training logs/MLflow points",
    )
    parser.add_argument(
        "--eval-strategy",
        type=str,
        default="epoch",
        choices=["no", "steps", "epoch"],
        help="Evaluation cadence strategy",
    )
    parser.add_argument(
        "--eval-steps",
        type=int,
        default=None,
        help="Evaluation frequency when --eval-strategy=steps",
    )
    parser.add_argument(
        "--use-4bit",
        action="store_true",
        default=True,
        help="Use 4-bit quantization (QLoRA)",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=2000,
        help="Maximum number of samples to use",
    )
    parser.add_argument(
        "--test-prompt",
        type=str,
        default="Explain the concept of machine learning in simple terms.",
        help="Test prompt for inference after training",
    )
    parser.add_argument(
        "--skip-inference",
        action="store_true",
        help="Skip inference test after training",
    )
    args = parser.parse_args()

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("ERROR: HF_TOKEN environment variable not set!")
        print("Please set it with: export HF_TOKEN='your_token_here'")
        sys.exit(1)

    world_size = int(os.environ.get("WORLD_SIZE", 1))
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    process_device_index = resolve_process_device_index(local_rank)

    if is_main_process():
        print("=" * 80)
        print("FINE-TUNING CONFIGURATION")
        print("=" * 80)
        print(f"Base Model: {args.model}")
        print(f"Output Directory: {args.output_dir}")
        print(f"Number of Epochs: {args.num_epochs}")
        print(f"Batch Size per GPU: {args.batch_size}")
        print(f"Gradient Accumulation Steps: {args.gradient_accum_steps}")
        print(f"Learning Rate: {args.learning_rate}")
        print(f"Logging Steps: {args.logging_steps}")
        print(f"Eval Strategy: {args.eval_strategy}")
        if args.eval_steps is not None:
            print(f"Eval Steps: {args.eval_steps}")
        print(f"Max Sequence Length: {args.max_seq_length}")
        print(f"4-bit Quantization: {args.use_4bit}")
        print()
        print(f"Distributed Training: {is_distributed()}")
        if is_distributed():
            print(f"World Size (Total GPUs): {world_size}")
            print(f"Visible CUDA devices in process: {torch.cuda.device_count()}")
            print(
                "CUDA_VISIBLE_DEVICES: "
                f"{os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>')}"
            )
            print(
                "Effective Batch Size: "
                f"{args.batch_size * args.gradient_accum_steps * world_size}"
            )
        else:
            print(f"Effective Batch Size: {args.batch_size * args.gradient_accum_steps}")
        print("=" * 80)

    if args.use_4bit:
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
    else:
        bnb_config = None

    if torch.cuda.is_available():
        torch.cuda.set_device(process_device_index)

    if is_distributed():
        device_map = {"": process_device_index}
    else:
        device_map = "auto"

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=bnb_config,
        device_map=device_map,
        token=hf_token,
        trust_remote_code=True,
    )

    model.config.use_cache = False
    model.config.pretraining_tp = 1

    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        token=hf_token,
        trust_remote_code=True,
    )
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    if args.use_4bit:
        model = prepare_model_for_kbit_training(model)

    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )

    dataset = load_dataset("WizardLM/WizardLM_evol_instruct_70k", split="train")

    if args.max_samples is not None:
        dataset = dataset.select(range(min(args.max_samples, len(dataset))))

    if is_main_process():
        print(f"Dataset size: {len(dataset)}")
        print(f"Sample instruction: {dataset[0]['instruction'][:100]}...")

    def formatting_func(example):
        return {"text": format_instruction(example)}

    dataset = dataset.map(formatting_func, remove_columns=dataset.column_names)
    dataset = dataset.train_test_split(test_size=0.1, seed=42)
    train_dataset = dataset["train"]
    eval_dataset = dataset["test"]

    training_kwargs = {
        "output_dir": args.output_dir,
        "num_train_epochs": args.num_epochs,
        "per_device_train_batch_size": args.batch_size,
        "per_device_eval_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.gradient_accum_steps,
        "learning_rate": args.learning_rate,
        "lr_scheduler_type": "cosine",
        "warmup_steps": max(1, int(len(train_dataset) * 0.03)),
        "logging_steps": args.logging_steps,
        "save_strategy": "epoch",
        "fp16": not torch.cuda.is_bf16_supported(),
        "bf16": torch.cuda.is_bf16_supported(),
        "optim": "paged_adamw_32bit",
        "gradient_checkpointing": True,
        "report_to": "none",
        "ddp_find_unused_parameters": False,
        "ddp_backend": "nccl" if is_distributed() else None,
        "local_rank": local_rank if is_distributed() else -1,
    }
    training_args_signature = inspect.signature(TrainingArguments.__init__)
    if "eval_strategy" in training_args_signature.parameters:
        training_kwargs["eval_strategy"] = args.eval_strategy
        if args.eval_strategy == "steps" and args.eval_steps is not None:
            training_kwargs["eval_steps"] = args.eval_steps
    elif "evaluation_strategy" in training_args_signature.parameters:
        training_kwargs["evaluation_strategy"] = args.eval_strategy
        if args.eval_strategy == "steps" and args.eval_steps is not None:
            training_kwargs["eval_steps"] = args.eval_steps
    if "group_by_length" in training_args_signature.parameters:
        training_kwargs["group_by_length"] = True

    training_args = TrainingArguments(**training_kwargs)

    trainer_kwargs = {
        "model": model,
        "train_dataset": train_dataset,
        "eval_dataset": eval_dataset,
        "peft_config": peft_config,
        "args": training_args,
    }
    sft_signature = inspect.signature(SFTTrainer.__init__)
    if "dataset_text_field" in sft_signature.parameters:
        trainer_kwargs["dataset_text_field"] = "text"
    elif "formatting_func" in sft_signature.parameters:
        trainer_kwargs["formatting_func"] = lambda example: example["text"]
    if "max_seq_length" in sft_signature.parameters:
        trainer_kwargs["max_seq_length"] = args.max_seq_length
    if "processing_class" in sft_signature.parameters:
        trainer_kwargs["processing_class"] = tokenizer
    elif "tokenizer" in sft_signature.parameters:
        trainer_kwargs["tokenizer"] = tokenizer
    if "packing" in sft_signature.parameters:
        trainer_kwargs["packing"] = False

    trainer = SFTTrainer(**trainer_kwargs)

    mlflow_active = is_mlflow_enabled() and is_main_process()
    if mlflow_active:
        mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
        run_name = os.getenv("MLFLOW_RUN_NAME", "llama7b-finetune")
        mlflow.start_run(run_name=run_name)
        mlflow.log_params(
            {
                "model": args.model,
                "output_dir": args.output_dir,
                "num_epochs": args.num_epochs,
                "batch_size": args.batch_size,
                "gradient_accum_steps": args.gradient_accum_steps,
                "learning_rate": args.learning_rate,
                "logging_steps": args.logging_steps,
                "eval_strategy": args.eval_strategy,
                "eval_steps": args.eval_steps,
                "max_seq_length": args.max_seq_length,
                "max_samples": args.max_samples,
                "world_size": world_size,
                "distributed": is_distributed(),
            }
        )
        trainer.add_callback(MLflowMetricsCallback())

    trainer.train()
    eval_metrics = trainer.evaluate()
    if mlflow_active and eval_metrics:
        mlflow.log_metrics(
            {
                k: float(v)
                for k, v in eval_metrics.items()
                if isinstance(v, (int, float))
            }
        )
    trainer.save_model()
    tokenizer.save_pretrained(args.output_dir)
    if mlflow_active:
        mlflow.end_run()

    if not args.skip_inference and is_main_process():
        test_instruction = format_instruction(
            {"instruction": args.test_prompt, "output": ""}
        )
        inputs = tokenizer(test_instruction, return_tensors="pt").to(model.device)

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=256,
                temperature=0.7,
                top_p=0.9,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )
        response = tokenizer.decode(outputs[0], skip_special_tokens=True)
        print(response)

    if is_main_process():
        print("TRAINING COMPLETE")


if __name__ == "__main__":
    main()
