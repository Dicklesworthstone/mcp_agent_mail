find . -type f \( -name "*.py" -o -name "*.js" -o -name "*.html" \) -exec wc -l {} + | awk '{total += $1} END {print total}'
