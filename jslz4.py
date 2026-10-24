# small tool for decoding zen-sessions.jsonlz4, made with Gemini. Used when debugging space labels showing up.

import argparse
import sys
import os
import lz4.block

def decompress_jsonlz4(input_path, output_path=None):
    if not os.path.exists(input_path):
        print(f"Error: File '{input_path}' does not exist.")
        sys.exit(1)

    # Default output filename if not specified
    if not output_path:
        base_name = os.path.splitext(input_path)[0]
        output_path = f"{base_name}.json" if base_name != input_path else f"{input_path}.decompressed.json"

    with open(input_path, "rb") as f:
        magic = f.read(8)
        if magic != b"mozLz40\0":
            print(f"Error: '{input_path}' does not have a valid Firefox jsonlz4 header.")
            sys.exit(1)
        
        compressed_bytes = f.read()
        decompressed_bytes = lz4.block.decompress(compressed_bytes)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(decompressed_bytes.decode("utf-8"))

    print(f"Successfully decompressed:\n  Input:  {input_path}\n  Output: {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Decompress Firefox .jsonlz4 files.")
    parser.add_argument("input", nargs="?", help="Path to the .jsonlz4 file")
    parser.add_argument("-o", "--output", help="Optional path for output .json file")

    args = parser.parse_args()

    # Prompt interactively if path wasn't provided via CLI argument
    file_input = args.input or input("Enter path to .jsonlz4 file: ").strip('"\' ')
    
    decompress_jsonlz4(file_input, args.output)
