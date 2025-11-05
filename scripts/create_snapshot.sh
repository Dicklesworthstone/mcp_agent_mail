#!/bin/bash

# This script creates 5 text file snapshots of all text files tracked in the git repository.
# Files are distributed evenly across the 5 output files.
# Binary files are automatically detected and skipped.

# Define the base name for output files. We'll create 5 files.
OUTPUT_BASE="repo_snapshot"
NUM_FILES=5

# Start with a clean slate by overwriting files if they exist.
echo "Creating repository snapshot split into $NUM_FILES files..."

# Initialize all output files
for i in $(seq 1 $NUM_FILES); do
    OUTPUT_FILE="${OUTPUT_BASE}_part${i}.txt"
    echo "Generated on: $(date)" > "$OUTPUT_FILE"
    echo "Part $i of $NUM_FILES" >> "$OUTPUT_FILE"
    echo "" >> "$OUTPUT_FILE"
done

# Get only files tracked by git (checked into repository).
# This automatically excludes .git directory, ignored files, and untracked files.
# For each file found, run a loop.
file_counter=0
git ls-files | grep -v "^${OUTPUT_BASE}_part[0-9]*.txt$" | while read -r filepath; do

    # Calculate which output file to use (cycle through 1-5)
    file_index=$((file_counter % NUM_FILES + 1))
    CURRENT_OUTPUT="${OUTPUT_BASE}_part${file_index}.txt"

    filename=$(basename "$filepath")

    # Print a clear separator and the file metadata to the current output file.
    echo "--- FILE START ---" >> "$CURRENT_OUTPUT"
    echo "Location: $filepath" >> "$CURRENT_OUTPUT"
    echo "Name: $filename" >> "$CURRENT_OUTPUT"
    echo "--- CONTENT ---" >> "$CURRENT_OUTPUT"

    # Check if the file is binary using the file command
    if file "$filepath" | grep -q "text"; then
        # File is text, append the actual content
        cat "$filepath" >> "$CURRENT_OUTPUT"
    else
        # File is binary, skip content
        echo "--- CONTENT SKIPPED (Binary File) ---" >> "$CURRENT_OUTPUT"
    fi

    # Print a separator at the end of the file content for clarity.
    # The two blank lines make the final text file easier to read.
    echo "" >> "$CURRENT_OUTPUT"
    echo "--- FILE END ---" >> "$CURRENT_OUTPUT"
    echo "" >> "$CURRENT_OUTPUT"

    # Increment the file counter
    file_counter=$((file_counter + 1))
done

echo "Snapshot complete."
echo "Created $NUM_FILES files:"
for i in $(seq 1 $NUM_FILES); do
    OUTPUT_FILE="${OUTPUT_BASE}_part${i}.txt"
    echo "  - $OUTPUT_FILE"
done
echo "Please upload these files so I can analyze the state of the repository."
