#!/usr/bin/env bash
# Restart the running bots with exactly the command line each was started
# with (a config change only takes effect after a restart).
#
#   bash tools/restart.sh            # every running market in this folder
#   bash tools/restart.sh btc eth    # only these markets
#   bash tools/restart.sh --list     # show what is running, change nothing
#
# For each running "main.py --symbol X" started from this folder it reads
# the command line and output file from /proc, asks the bot to stop
# (SIGTERM, the same clean shutdown as Ctrl-C), waits until it is gone, and
# starts it again in the background with nohup. If a bot does not stop
# within 60 s it is left alone and NOT started a second time (two bots on
# one market break each other's orders). Two bots found on one market are
# both stopped and one is started.
#
# Run it from the same shell setup you start the bots from (same folder,
# virtualenv activated). A bot that was showing the dashboard in a terminal
# or screen comes back as a background process writing plain logs.
#
# 中文：按原来的启动命令重启正在运行的机器人（改了配置必须重启才生效）。
# 不带参数 = 本目录下所有在跑的市场；后面跟币种 = 只重启这些；--list = 只看
# 不动。先发 SIGTERM 正常退出，等它完全停掉再用 nohup 后台启动；60 秒内停不
# 掉就不再启动第二个（同一市场两个进程会互相破坏订单）。请在平时启动机器人
# 的同一个目录、已激活虚拟环境的终端里运行。
set -u
cd "$(dirname "$0")/.." || exit 1
here=$(pwd -P)
list_only=0
want=()
for a in "$@"; do
  case "$a" in
    --list) list_only=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) want+=("$(printf '%s' "$a" | tr '[:lower:]' '[:upper:]' | sed 's/\.YAML$//')") ;;
  esac
done

declare -A cmd_of out_of pids_of
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ -r "$d/cmdline" ] || continue
  mapfile -d '' -t argv < "$d/cmdline" 2>/dev/null || continue
  [ "${#argv[@]}" -ge 3 ] || continue
  is_bot=0; sym=""
  for i in "${!argv[@]}"; do
    case "${argv[$i]}" in
      main.py|*/main.py) is_bot=1 ;;
      --symbol) sym=${argv[$((i+1))]:-} ;;
      --symbol=*) sym=${argv[$i]#--symbol=} ;;
    esac
  done
  [ "$is_bot" = 1 ] && [ -n "$sym" ] || continue
  [ "$(readlink "$d/cwd" 2>/dev/null)" = "$here" ] || continue
  sym=$(printf '%s' "$sym" | tr '[:lower:]' '[:upper:]')
  if [ "${#want[@]}" -gt 0 ]; then
    hit=0; for w in "${want[@]}"; do [ "$w" = "$sym" ] && hit=1; done
    [ "$hit" = 1 ] || continue
  fi
  pids_of[$sym]="${pids_of[$sym]:-} $pid"
  # same interpreter as before. A virtualenv's python is a symlink to the
  # system python and only acts as the venv when started through that link,
  # so never resolve the link: if argv[0] is a bare name (python3), look it
  # up in the PATH the bot itself was started with (the activated venv).
  case "${argv[0]}" in
    /*) ;;
    */*) argv[0]="$(readlink "$d/cwd")/${argv[0]}" ;;
    *)
      ppath=$(tr '\0' '\n' < "$d/environ" 2>/dev/null | sed -n 's/^PATH=//p' | head -1)
      found=""
      if [ -n "$ppath" ]; then
        IFS=: read -r -a pdirs <<< "$ppath"
        for pd in "${pdirs[@]}"; do
          case "$pd" in /*) ;; *) pd="$(readlink "$d/cwd")/$pd" ;; esac
          if [ -x "$pd/${argv[0]}" ]; then found="$pd/${argv[0]}"; break; fi
        done
      fi
      [ -n "$found" ] && argv[0]=$found
      ;;
  esac
  printf -v q '%q ' "${argv[@]}"
  cmd_of[$sym]=$q
  out=$(readlink "$d/fd/1" 2>/dev/null || true)
  case "$out" in /dev/*|pipe:*|socket:*|"") out="" ;; *" (deleted)") out="" ;; esac
  out_of[$sym]=$out
done

if [ "${#pids_of[@]}" -eq 0 ]; then
  echo "no running bot found in $here / 这个目录下没有找到正在运行的机器人"
  exit 1
fi

syms=$(printf '%s\n' "${!pids_of[@]}" | sort)
if [ "$list_only" = 1 ]; then
  for s in $syms; do echo "$s: pid${pids_of[$s]} | ${cmd_of[$s]}"; done
  exit 0
fi

ok=0; bad=0
for s in $syms; do
  pids=${pids_of[$s]}
  n=$(echo $pids | wc -w)
  [ "$n" -gt 1 ] && echo "$s: $n processes on this market, stopping all, starting one / 同一市场有 $n 个进程，全部停掉后只启动一个"
  kill -TERM $pids 2>/dev/null
  gone=0
  for _ in $(seq 120); do
    alive=0; for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" = 0 ] && { gone=1; break; }
    sleep 0.5
  done
  if [ "$gone" != 1 ]; then
    echo "$s: did NOT stop within 60 s (pid$pids) — not started again, check it by hand / 60 秒内没有停下，未重新启动，请手动检查"
    bad=$((bad+1)); continue
  fi
  out=${out_of[$s]}
  if [ -z "$out" ]; then
    lower=$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')
    mkdir -p "logs/$lower"; out="logs/$lower/nohup.out"
  fi
  eval "nohup ${cmd_of[$s]} >> $(printf '%q' "$out") 2>&1 < /dev/null &"
  new=$!
  sleep 3
  if kill -0 "$new" 2>/dev/null; then
    echo "$s: restarted, pid $new, output -> $out / 已重启"
    ok=$((ok+1))
  else
    echo "$s: FAILED to start, see $out / 启动失败，请看 $out"
    echo "    command: ${cmd_of[$s]}"
    tail -n 5 "$out" 2>/dev/null | sed 's/^/    | /'
    bad=$((bad+1))
  fi
done
echo
echo "$ok restarted, $bad need attention / 已重启 $ok 个，$bad 个需要处理"
[ "$bad" = 0 ]
