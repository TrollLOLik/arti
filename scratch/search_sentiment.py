import os
import glob

def main():
    print("Searching for sentiment...")
    for f in glob.glob("**/*.py", recursive=True):
        if "venv" in f or ".venv" in f:
            continue
        try:
            with open(f, "r", encoding="utf-8") as file:
                for idx, line in enumerate(file):
                    if "sentiment" in line.lower():
                        print(f"{f}:{idx+1} - {line.strip()}")
        except Exception as e:
            pass

if __name__ == "__main__":
    main()
