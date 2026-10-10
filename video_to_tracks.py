"""Kept for the old command line. The camera agent now lives in the `agent` package:

  python -m agent --help
  python video_to_tracks.py --source clip.mp4 --calib calib.json --layout A --out tracks.csv   # same as:
  python -m agent run --source clip.mp4 --calib calib.json --layout A --csv tracks.csv
"""
import sys

from agent.cli import main

if __name__ == "__main__":
    argv = ["run"] + ["--csv" if a == "--out" else a for a in sys.argv[1:]]
    main(argv)
