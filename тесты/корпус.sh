#!/bin/sh
# Замер переводимости на РЕАЛЬНОМ корпусе C (не входит в обязательный прогон.sh).
#   1) kfc --без-проверки по каждому .c каталога (флаги — автоподхват из
#      compile_commands.json, как при обычном запуске kfc);
#   2) транспилятор --только-си по каждому .конда — ВСЕ диагностики разом;
#   3) сводка: сколько файлов kfc перевёл, сколько ПРОШЛИ транспилятор, пометки
#      по кодам, топ диагностик транспилятора (нормализованных).
# База «до/после» для каждого шага повышения переводимости.
#
# Использование: sh тесты/корпус.sh КАТАЛОГ_С_ФАЙЛАМИ_C [КАТАЛОГ_ВЫВОДА]
#   напр.: sh тесты/корпус.sh ~/Документы/weston/clients
# Транспилятор — KONDA_TRANSPILER или соседний ../ТранспиляторКонда (собирается).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:?укажите каталог с .c (напр. weston/clients)}"
OUT="${2:-$(mktemp -d)}"
TR="${KONDA_TRANSPILER:-$ROOT/../ТранспиляторКонда}"
TRBIN="$TR/Собранное/ТранспиляторКонда"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
mkdir -p "$OUT"
make -C "$TR" >/dev/null 2>&1 || { echo "ОШИБКА: транспилятор не собрался в «$TR»"; exit 1; }

# Шаг 1+2 на файл: kfc → отчёт JSON; транспилятор → лог диагностик. Имена
# переменных — ASCII (dash).
export ROOT OUT TRBIN
find "$SRC" -maxdepth 1 -name '*.c' -print0 | xargs -0 -P "$JOBS" -I{} sh -c '
    f="$1"; b=$(basename "$f" .c)
    ( cd "$(dirname "$f")" && timeout 300 python3 "$ROOT/kfc.py" --без-проверки "$f" \
        -o "$OUT/$b.конда" --отчёт "$OUT/$b.json" >"$OUT/$b.kfc.log" 2>&1 ) || true
    if [ -f "$OUT/$b.конда" ]; then
        # Флаги C (-I/-D, §129 транспилятора) — из отчёта kfc: те же, что видел clang.
        ( cd "$OUT" && python3 -c "import json,os,sys
ф = json.load(open(sys.argv[1], encoding=\"utf-8\")).get(\"флаги_транспилятора\", [])
os.execvp(sys.argv[2], [sys.argv[2]] + ф + sys.argv[3:])" \
            "$OUT/$b.json" "$TRBIN" --только-си "$b.конда" >"$OUT/$b.tr.log" 2>&1 \
            && echo ok >"$OUT/$b.tr.ok" ) || true
    fi' _ {}

python3 - "$SRC" "$OUT" <<'PY'
import collections, glob, json, os, re, sys
src, out = sys.argv[1], sys.argv[2]
всего = sorted(os.path.basename(п)[:-2] for п in glob.glob(os.path.join(src, "*.c")))
переведено = [б for б in всего if os.path.exists(os.path.join(out, б + ".конда"))]
прошли = [б for б in всего if os.path.exists(os.path.join(out, б + ".tr.ok"))]
коды, диаг = collections.Counter(), collections.Counter()
for б in переведено:
    п = os.path.join(out, б + ".json")
    if os.path.exists(п):
        for з in json.load(open(п, encoding="utf-8")).get("задачи", []):
            коды[з["код"]] += 1
    л = os.path.join(out, б + ".tr.log")
    if os.path.exists(л):
        for стр in open(л, encoding="utf-8", errors="replace"):
            м = re.match(r"^Ошибка транспиляции [^)]*\): (.*)", стр)
            if м:
                диаг[re.sub(r"[0-9]+", "N", re.sub(r"«[^»]*»", "«…»", м.group(1)))[:90]] += 1
print(f"файлов: {len(всего)}; kfc перевёл: {len(переведено)}; "
      f"прошли транспилятор: {len(прошли)}")
print(f"пометок kfc: {sum(коды.values())}")
for к, ч in коды.most_common():
    print(f"  {ч:5} {к}")
print(f"ошибок транспилятора: {sum(диаг.values())}")
for к, ч in диаг.most_common(20):
    print(f"  {ч:5} {к}")
не = [б for б in всего if б not in переведено]
if не:
    print("kfc не перевёл:", " ".join(не))
print(f"вывод: {out}")
PY
