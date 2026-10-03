#!/bin/bash -e
compile_commands=$1
input=$2
output=$3
dep=$4
# 第一阶段:用 compile_commands.json 里的编译参数汇编 offsets.c。
# shlex.split 是因为 meson 会给含空格的参数加引号(路径带空格时 naive split 会碎)。
python3 - "$compile_commands" "$input" "$output" "$dep" "$(dirname "$0")/staticdefine.h" <<'END'
import json, shlex, subprocess, sys
cc_json, input, output, dep, header = sys.argv[1:6]
with open(cc_json) as f:
    commands = json.load(f)
for command in commands:
    if command['file'].endswith('asbestos/asbestos.c'):
        break
args = shlex.split(command['command'])[:-9]
args += ['-MD', '-MQ', output, '-MF', dep]
args += [input, '-include', header, '-S', '-o', output + '.s']
p = subprocess.run(args, cwd=command.get('directory'))
sys.exit(p.returncode)
END
sed -ne 's:^[[:space:]]*\.ascii[[:space:]]*"\(.*\)".*:\1:;
         /^->/{s:->#\(.*\):/* \1 */:;
         s:^->\([^ ]*\) [\$$#]*\([^ ]*\) \(.*\):#define \1 \2 /* \3 */:;
         s:->::; p;}' < "$output.s" > "$output"
# sed magic was copied from the linux kernel build system
