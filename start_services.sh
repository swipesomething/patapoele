#!/bin/bash

# Start main.py in the background
python main.py &

# Keep the container running
tail -f /dev/null
