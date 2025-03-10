import argparse
import concurrent.futures
import numpy as np
import os
import psutil
import subprocess
import torch
import shutil
import transformers

from ast import literal_eval
from datetime import datetime
from transformers import AutoModelForCausalLM

class NoInit:
    def __enter__(self):
        def noop(*args, **kwargs):
            pass

        (k, u, n) = (
            torch.nn.init.kaiming_uniform_,
            torch.nn.init.uniform_,
            torch.nn.init.normal_,
        )
        torch.nn.init.kaiming_uniform_ = noop
        torch.nn.init.uniform_ = noop
        torch.nn.init.normal_ = noop

        transformers.modeling_utils._init_weights = False
        self.funcs = (k, u, n)

    def __exit__(self, *args):
        (k, u, n) = self.funcs
        (
            torch.nn.init.kaiming_uniform_,
            torch.nn.init.uniform_,
            torch.nn.init.normal_,
        ) = (
            k,
            u,
            n,
        )
        transformers.modeling_utils._init_weights = True


def clear_console():
    if os.name == "nt":  # For Windows
        subprocess.call("cls", shell=True)
    else:  # For Linux and macOS
        subprocess.call("clear", shell=True)
        

def merge_models(model1, model2, gradient_values, layer_only=False, no_layers=False, args=None):
    """
    Merge two models by blending their state_dicts based on a smoothly interpolated list of gradient values.

    Args:
    - model1: The first model object to merge.
    - model2: The second model object to merge.
    - gradient_values: List of gradient values. e.g. [1.0, 0.5, 0.0]
    - layer_only: If True, only process tensors with keys containing "layer".
    """

    # No Torch gradients needed since we're only adjusting the weights and not training
    with torch.no_grad():

        # Get the state_dicts of both models
        state_dict1 = model1.state_dict()
        state_dict2 = model2.state_dict()
        
        # Filter keys if layer_only is True
        if layer_only:
            keys = [key for key in state_dict1.keys() if "layer" in key]
        elif no_layers:
            keys = [key for key in state_dict1.keys() if "layer" not in key]
        else:
            keys = state_dict1.keys()
            
        if args.custom_filter:
            keys = [key for key in keys if args.custom_filter in key]
            
        # Function to merge tensors when vocab sizes differ
        def merge_vocab_tensors(tensor1, tensor2, blend_ratio):
            vocab_size1 = tensor1.shape[0]
            vocab_size2 = tensor2.shape[0]
            min_vocab_size = min(vocab_size1, vocab_size2)
        
            # Create a new tensor based on the vocab size of model1
            new_tensor = torch.zeros(vocab_size1, tensor1.shape[1], dtype=tensor1.dtype).to(tensor1.device)
        
            # Blend the values for the common vocab size
            new_tensor[:min_vocab_size, :] = tensor1[:min_vocab_size, :] * (1 - blend_ratio) + tensor2[:min_vocab_size, :] * blend_ratio
        
            # If there are unique tokens in tensor1, retain their original values
            if vocab_size1 > min_vocab_size:
                new_tensor[min_vocab_size:, :] = tensor1[min_vocab_size:, :]
        
            return new_tensor    

        # Calculate the sections based on gradient values
        sections = len(gradient_values) - 1
        tensors_per_section = len(keys) // sections

        # Generate a smoothly interpolated list of blend ratios for the entire model
        blend_ratios = []
        for i in range(sections):
            start_value = gradient_values[i]
            end_value = gradient_values[i + 1]
            blend_ratios.extend(np.linspace(start_value, end_value, tensors_per_section))

        # Adjust if there's a remainder
        remainder = len(keys) - len(blend_ratios)
        if remainder:
            blend_ratios.extend([gradient_values[-1]] * remainder)

        # Loop through the keys to merge the tensors
        for idx, key in enumerate(keys):
            # Get blend ratio for the current tensor
            ratio_model2 = blend_ratios[idx]
            ratio_model1 = 1 - ratio_model2

            # If the tensor is one of those with differing vocab sizes
            if key in ["lm_head.weight", "model.embed_tokens.weight"]:
                state_dict1[key] = merge_vocab_tensors(state_dict1[key], state_dict2[key], ratio_model2)
            else:
                # Blend the tensors using the blend ratios
                state_dict1[key] = (ratio_model1 * state_dict1[key] + ratio_model2 * state_dict2[key])

            # Print log of blending ratios for current tensor
            print(f"{datetime.now().strftime('%H:%M:%S')} - Merging tensor {key} ({idx}/{len(keys)}) ({round(ratio_model1, 2)} - {round(ratio_model2, 2)})")

        # Load the blended state_dict to the first model
        model1.load_state_dict(state_dict1)


def main(args):
    clear_console()
    print(f"{datetime.now().strftime('%H:%M:%S')} - Starting script, please wait...")

    with torch.no_grad():
        torch.set_default_dtype(torch.float32)

        # Using swap memory for the process (Unless you have 128 GB RAM...)
        device = torch.device("cpu")
        print(device)

        with NoInit():
            # Load Model 1
            print(f"{datetime.now().strftime('%H:%M:%S')} - Loading Model 1 ({args.model_path1})...")
            model1 = AutoModelForCausalLM.from_pretrained(args.model_path1, low_cpu_mem_usage=True)
            model1.half()
            model1 = model1.to(device)
            model1.eval()
            print(f"Model 1 Loaded. Dtype: {model1.dtype}")
    
            # Load Model 2
            print(f"{datetime.now().strftime('%H:%M:%S')} - Loading Model 2 ({args.model_path2})...")
            model2 = AutoModelForCausalLM.from_pretrained(args.model_path2, low_cpu_mem_usage=True)
            model2.half()
            model2 = model2.to(device)
            model2.eval()
            print(f"{datetime.now().strftime('%H:%M:%S')} -  Model 2 Loaded. Dtype: {model2.dtype}")

        # Merge the models
        print(f"{datetime.now().strftime('%H:%M:%S')} - Merging models...")
        merge_models(model1, model2, args.gradient_values, args.layer_only, args.no_layers, args=args)

        if args.output_model_path:
            print(f"{datetime.now().strftime('%H:%M:%S')} - Saving new model...")
            model1.save_pretrained(args.output_model_path, max_shard_size=args.max_shard_size)

            print(f"{datetime.now().strftime('%H:%M:%S')} - Saved to: {args.output_model_path}")
            print(f"{datetime.now().strftime('%H:%M:%S')} - Copying files to: {args.output_model_path}")
            files_to_copy = [
                "added_tokens.json",
                "tokenizer.model",
                "special_tokens_map.json",
                "tokenizer_config.json",
                "vocab.json",
                "merges.txt"
            ]

            for filename in files_to_copy:
                src_path = os.path.join(args.model_path1, filename)
                dst_path = os.path.join(args.output_model_path, filename)
                try:
                    shutil.copy2(src_path, dst_path)
                except FileNotFoundError:
                    print(f"File {filename} not found in {args.model_path1}. Skipping.")

        print(f"{datetime.now().strftime('%H:%M:%S')} - Script Completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Merge Models')
    parser.add_argument('--model_path1', type=str, required=True, help='Path to first model')
    parser.add_argument('--model_path2', type=str, required=True, help='Path to second model')
    parser.add_argument('--output_model_path', type=str, required=True, help='Output path for the merged model')
    parser.add_argument('--gradient_values', type=literal_eval, required=True, help='List of gradient values. e.g. [1.0, 0.5, 0.0]')
    parser.add_argument('--max_shard_size', type=str, default="2000MiB", help='Output shard size')
    parser.add_argument('--layer_only', action='store_true', help='If set, only process tensors with keys containing "layer"')
    parser.add_argument('--no_layers', action='store_true', help='If set, only process tensors with keys NOT containing "layer"')
    parser.add_argument('--custom_filter', type=str, default=None, help='Filter tensors by a custom substring in their names')
    
    args = parser.parse_args()
    main(args)
