import torch
import argparse
import os

os.environ["CUDA_VISIBLE_DEVICES"] = "1"
print(f"CUDA_VISIBLE_DEVICES set to: {os.environ.get('CUDA_VISIBLE_DEVICES')}")

import numpy as np
import evaluate
from transformers import WhisperProcessor, WhisperForConditionalGeneration, Seq2SeqTrainingArguments, Seq2SeqTrainer
from peft import prepare_model_for_kbit_training, LoraConfig, get_peft_model
from dataclasses import dataclass
from typing import Any, Dict, List, Union
from load_datasets import load_process_datasets
from typing import Any, Dict, List, Union

from huggingface_hub import login

if torch.cuda.is_available():
    print(f"PyTorch can access {torch.cuda.device_count()} GPU(s).")
    print(f"Current CUDA device: {torch.cuda.current_device()} ({torch.cuda.get_device_name(torch.cuda.current_device())})")
else:
    print("PyTorch cannot access any GPUs. CUDA is not available.")

try:
    login(token=hf_token)
    print("Successfully logged in to Hugging Face Hub.")
except Exception as e:
    print(f"Failed to login to Hugging Face Hub: {e}")

metric = evaluate.load("wer")

def compute_metrics(pred):
    pred_ids = pred.predictions
    label_ids = pred.label_ids

    # replace -100 with the pad_token_id
    label_ids[label_ids == -100] = processor.tokenizer.pad_token_id

    # we do not want to group tokens when computing the metrics
    pred_str = processor.batch_decode(pred_ids, skip_special_tokens=True)
    label_str = processor.batch_decode(label_ids, skip_special_tokens=True)

    wer = 100 * metric.compute(predictions=pred_str, references=label_str)

    return {"wer": wer}

@dataclass
class DataCollatorSpeechSeq2SeqWithPadding:
    processor: Any

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        input_features = [{"input_features": feature["input_features"]}
                          for feature in features]
        batch = self.processor.feature_extractor.pad(
            input_features, return_tensors="pt")

        # get the tokenized label sequences
        label_features = [{"input_ids": feature["labels"]}
                          for feature in features]
        # pad the labels to max length
        labels_batch = self.processor.tokenizer.pad(
            label_features, return_tensors="pt")

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(
            labels_batch.attention_mask.ne(1), -100)

        # if bos token is appended in previous tokenization step,
        # cut bos token here as it's append later anyways
        if (labels[:, 0] == self.processor.tokenizer.bos_token_id).all().cpu().item():
            labels = labels[:, 1:]

        batch["labels"] = labels

        return batch

datasets_settings = [
#    ["mdcc", {}],
#    ["common_voice", {"language_abbr": "zh-HK"}],
#    ["aishell_1", {}],
#    ["thchs_30", {}],
#    ["magicdata", {}],
    ["ohkoonza/Whisper_pharmacy", {}]
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Model setups
    parser.add_argument("--model_id", default="th-medium-combined", type=str)
    parser.add_argument("--task", default="transcribe", type=str)
    parser.add_argument("--language", default="th", type=str)
    parser.add_argument("--device", default="cuda:0", type=str)
    parser.add_argument("--max_new_tokens", default=225, type=int)
    # Dataset setups
    parser.add_argument("--num_test_samples", default=1000, type=int)
    parser.add_argument("--max_input_length", default=30.0, type=float)
    parser.add_argument("--streaming", default=False, type=bool)
    parser.add_argument("--num_proc", default=4, type=int)
    # LoRA setups
    parser.add_argument("--r", default=32, type=int)
    parser.add_argument("--lora_alpha", default=64, type=int)
    parser.add_argument("--lora_dropout", default=0.05, type=float)
    # Finetuning setups
    parser.add_argument("--learning_rate", default=1e-3, type=float)
    parser.add_argument("--gradient_accumulation_steps", default=16, type=int)
    parser.add_argument("--train_batch_size", default=8, type=int)
    parser.add_argument("--eval_batch_size", default=8, type=int)
    parser.add_argument("--fp16", default=True, type=bool)
    parser.add_argument("--kbit_training", default=True, action="store_true")
    parser.add_argument("--warmup_steps", default=50, type=int)
    #parser.add_argument("--max_steps", default=320, type=int)
    parser.add_argument("--num_train_epochs" , default=10 ,type=int)
    parser.add_argument("--save_steps", default=100, type=int)
    parser.add_argument("--eval_steps", default=100, type=int)
    parser.add_argument("--logging_steps", default=25, type=int)

    args = parser.parse_args()
    print(f"Settings: {args}")

    experiment_name = f"whisper-{args.model_id}-lora-experiment"

    # Load pretrained processor
    model_name_or_path = f"biodatlab/whisper-{args.model_id}"
    processor = WhisperProcessor.from_pretrained(
        model_name_or_path, language=args.language, task=args.task)

    ds = load_process_datasets(
        datasets_settings,
        processor,
        max_input_length=args.max_input_length,
        num_test_samples=args.num_test_samples,
        streaming=args.streaming,
        num_proc=args.num_proc,
        use_full_test_set=True
    )
    print("train sample: ", next(iter(ds["train"])))
    print("test sample: ", next(iter(ds["test"])))

    data_collator = DataCollatorSpeechSeq2SeqWithPadding(processor=processor)

    # TODO 8-bit training and inference very slow
    model = WhisperForConditionalGeneration.from_pretrained(
        model_name_or_path,
        load_in_8bit=args.kbit_training,
        device_map=args.device,
    )
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []
    if args.kbit_training:
        model = prepare_model_for_kbit_training(model)
        args.fp16 = False

    config = LoraConfig(r=args.r, lora_alpha=args.lora_alpha,
                        target_modules=["q_proj", "v_proj"], lora_dropout=args.lora_dropout, bias="none")
    model = get_peft_model(model, config)
    model.print_trainable_parameters()

    training_args = Seq2SeqTrainingArguments(
        output_dir="./logs/" + experiment_name+"_2",  # change to a repo name of your choice
        per_device_train_batch_size=args.train_batch_size,
        # increase by 2x for every 2x decrease in batch size
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        warmup_steps=args.warmup_steps,
        #max_steps=args.max_steps,
        evaluation_strategy="steps",
        # gradient_checkpointing=True,
        # optim="adamw_torch",
        num_train_epochs=args.num_train_epochs,
        predict_with_generate=True,
        load_best_model_at_end=True,
        metric_for_best_model="wer",
        greater_is_better=False,
        fp16=args.fp16,
        dataloader_num_workers=2,
        per_device_eval_batch_size=args.eval_batch_size,
        generation_max_length=args.max_new_tokens,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        logging_steps=args.logging_steps,
        report_to=["tensorboard"],
        # required as the PeftModel forward doesn't have the signature of the wrapped model's forward
        remove_unused_columns=False,
        label_names=["labels"],  # same reason as above
        push_to_hub=False,
    )

    trainer = Seq2SeqTrainer(
        args=training_args,
        model=model,
        train_dataset=ds["train"],
        eval_dataset=ds["test"],
        data_collator=data_collator,
        compute_metrics=compute_metrics,
        tokenizer=processor.feature_extractor,
    )

    processor.save_pretrained(training_args.output_dir)
    # silence the warnings. Please re-enable for inference!
    model.config.use_cache = False
    trainer.train()
