#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
konda-from-clang (kfc) — конвертер C → Konda через AST-дамп clang.

Цель: перевести как можно больше кода C так, чтобы результат ПРОШЁЛ транспилятор
Konda и работал как задумано. Небезопасные конструкции C (сырые указатели,
арифметика, reinterpret-cast, доступ к union) оборачиваются в «небезопасно { }»
там, где нужно; где возможно — переводятся в безопасные конструкции Konda
(массив/срез, malloc→выделить/срез). Что не выражается в Konda в принципе
(адрес-оф «&x», goto, …) — помечается «// TODO(konda): …», чтобы человек или ИИ
доработали сегмент позже.

Запускает `clang -Xclang -ast-dump=json` внешним процессом (без линковки).
kfc НЕ делает проверок безопасности — их выполняет сам транспилятор над выводом.

ВАЖНО (решение пользователя): имена функций НЕ трогаем (без манглинга/
транслитерации). Исключение — точка входа `main` → `точка_входа` (соглашение).

Цикл проверки (по умолчанию включён): kfc генерирует перевод, прогоняет его
ПОДПРОЦЕССОМ через транспилятор Konda, читает диагностики и уточняет себя —
ослабляя безопасность только там, где транспилятор реально возражает. См.
`проверка.py`. Непочинённое превращается в пометки KONDA-TODO (`пометки.py`)
и, по флагу `--отчёт`, в машиночитаемый JSON для ИИ.

Использование:
    python3 kfc.py файл.c [-o вывод.конда] [--отчёт задачи.json]
                          [--без-проверки] [--итераций N]
                          [--транспилятор ПУТЬ] [-- <флаги clang>]
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys

import владение as влад
import нулевые as нул
import пометки as пм
import проверка as пров


class Политика:
    """Решения, ослабляющие безопасность. Пусто = самый безопасный перевод;
    цикл проверки наполняет её ТОЛЬКО по фактическим возражениям транспилятора."""

    def __init__(self):
        self.небезоп_узлы = set()      # id узлов-операторов → «небезопасно { … }»
        self.полные_обёртки = set()    # имена функций → всё тело в «небезопасно»
        self.срез_параметры = set()    # "функция:индекс" → параметр как «срез<T>»
        self.режим_ссылки = {}         # "функция:индекс" → «изменяемый»/«вывод»
        self.отмена_ссылки = set()     # "функция:индекс" → откат ref/out к указателю
        self.отмена_возможно = set()   # (функция, имя) → снять слабое «возможно»
        self.включить_возможно = set()  # (функция, имя) → принудительно «возможно»
        self.пометки_узлы = {}         # id узла → пм.Пометка (сдались)

    def отпечаток(self):
        return (frozenset(self.небезоп_узлы), frozenset(self.полные_обёртки),
                frozenset(self.срез_параметры), frozenset(self.пометки_узлы),
                frozenset(self.режим_ссылки.items()), frozenset(self.отмена_ссылки),
                frozenset(self.отмена_возможно), frozenset(self.включить_возможно))

# ─── маппинг типов C → Konda (эталон — конда_вывод.c транспилятора) ───────────
БАЗА_ТИПОВ = {
    "void": "ничего",
    "_Bool": "логический", "bool": "логический",
    "char": "символ", "signed char": "целое8", "unsigned char": "байт",
    "short": "целое16", "short int": "целое16",
    "int": "целое32", "long": "целое64", "long int": "целое64",
    "long long": "целое64", "long long int": "целое64",
    "float": "вещественное", "double": "вещественное64", "long double": "вещественное64",
    "int8_t": "целое8", "int16_t": "целое16", "int32_t": "целое32", "int64_t": "целое64",
    "uint8_t": "байт", "uint16_t": "целое16", "uint32_t": "целое32", "uint64_t": "целое64",
    "size_t": "целое64", "ssize_t": "целое64", "ptrdiff_t": "целое64",
    "intptr_t": "целое64", "uintptr_t": "целое64", "wchar_t": "целое32",
}
ВСЕГДА_БЕЗЗНАК = {"unsigned char", "uint8_t", "uint16_t", "uint32_t",
                  "uint64_t", "size_t", "uintptr_t"}


def раскодировать_строку(s: str) -> str:
    """clang октально экранирует не-ASCII байты (кириллица → \\320\\261…).
    Собираем такие байты обратно в UTF-8, сохраняя обычные escape'ы."""
    out, байты = [], bytearray()

    def сброс():
        if байты:
            out.append(байты.decode("utf-8", errors="replace"))
            байты.clear()

    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] in "01234567":
            j, восьм = i + 1, ""
            while j < len(s) and len(восьм) < 3 and s[j] in "01234567":
                восьм += s[j]
                j += 1
            байты.append(int(восьм, 8) & 0xFF)
            i = j
            continue
        сброс()
        out.append(c)
        i += 1
    сброс()
    return "".join(out)


def _строка_литерал_в_байты(значение: str) -> bytearray:
    """clang-значение StringLiteral («"..."» с октальными/обычными escape) →
    фактические БАЙТЫ (без завершающего NUL). Для «char s[N] = "..."» →
    литерал массива символов."""
    s = значение
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    байты = bytearray()
    простые = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, '"': 34,
               "'": 39, "a": 7, "b": 8, "f": 12, "v": 11}
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nc = s[i + 1]
            if nc in "01234567":                       # \NNN — октальный байт
                j, восьм = i + 1, ""
                while j < len(s) and len(восьм) < 3 and s[j] in "01234567":
                    восьм += s[j]; j += 1
                байты.append(int(восьм, 8) & 0xFF); i = j; continue
            if nc in простые:
                байты.append(простые[nc]); i += 2; continue
            байты.append(ord(nc) & 0xFF); i += 2; continue
        байты.extend(c.encode("utf-8"))
        i += 1
    return байты


def _байт_в_символ(b: int) -> str:
    """один байт → C-символьный литерал (печатный ASCII как есть, иначе escape)."""
    спец = {0: "'\\0'", 10: "'\\n'", 9: "'\\t'", 13: "'\\r'",
            92: "'\\\\'", 39: "'\\''"}
    if b in спец:
        return спец[b]
    if 32 <= b <= 126:
        return f"'{chr(b)}'"
    return f"'\\{b:03o}'"                               # октальный escape


_ШИРИНА_ЦЕЛОГО = {
    "char": 8, "signed char": 8, "unsigned char": 8, "_Bool": 8, "bool": 8,
    "short": 16, "short int": 16, "unsigned short": 16, "unsigned short int": 16,
    "int": 32, "unsigned int": 32, "unsigned": 32, "wchar_t": 32,
    "long": 64, "long int": 64, "unsigned long": 64, "unsigned long int": 64,
    "long long": 64, "long long int": 64, "unsigned long long": 64,
    "unsigned long long int": 64, "size_t": 64, "ssize_t": 64, "ptrdiff_t": 64,
    "intptr_t": 64, "uintptr_t": 64, "off_t": 64, "time_t": 64,
    "int8_t": 8, "uint8_t": 8, "int16_t": 16, "uint16_t": 16,
    "int32_t": 32, "uint32_t": 32, "int64_t": 64, "uint64_t": 64,
}


def _ширина_целого(qt: str):
    """Ширина ЦЕЛОГО C-типа в битах (указатель = 64), иначе None (не целое)."""
    t = без_квалификаторов(qt).strip()
    if "*" in t or "[" in t:
        return 64                                  # указатель/массив-указатель
    return _ШИРИНА_ЦЕЛОГО.get(t)


def qualtype(n) -> str:
    t = n.get("type")
    if isinstance(t, dict):
        return t.get("qualType", "int")
    if isinstance(t, str):
        return t
    return "int"


def без_квалификаторов(t: str) -> str:
    # Границы слов: иначе «const» внутри «constraints» → « raints» (порча имени
    # типа). Убираем только отдельные квалификаторы.
    t = re.sub(r"\b(const|volatile|restrict)\b", " ", t)
    return " ".join(t.split())


def конда_тип(qt: str) -> str:
    """Грубый перевод C-типа (qualType) в Konda-тип."""
    t = без_квалификаторов(qt).strip()
    if "(" in t and ")" in t and "(*" not in t:
        return "/*функц-тип*/ ничего"       # тип функции — вне охвата
    указатели = t.count("*")
    t = t.replace("*", " ")
    t = " ".join(t.split())
    if "[" in t:                              # массив как тип → указатель (приближение)
        t = t.split("[")[0].strip()
        указатели += 1
    беззнак = t in ВСЕГДА_БЕЗЗНАК or t.startswith("unsigned")
    ключ = t
    if t.startswith("unsigned "):
        ключ = t[len("unsigned "):].strip() or "int"
    if t == "unsigned":
        ключ = "int"
    if t.startswith("signed "):
        ключ = t[len("signed "):].strip() or "int"
    if t.startswith("void") and указатели > 0:
        return "символ" + "*" * указатели      # void* → символ* (приближение)
    имя = БАЗА_ТИПОВ.get(ключ)
    if имя is None:
        имя = (ключ.replace("struct ", "").replace("enum ", "")
               .replace("union ", "").strip()) or "целое32"
        беззнак = False
    префикс = "неподписанный " if (беззнак and имя.startswith("целое")) else ""
    return префикс + имя + "*" * указатели


def значение_по_умолчанию(kt: str):
    """Безопасное значение по умолчанию для temp-переменной (или None)."""
    if kt.endswith("*") or kt == "ничего":
        return None
    if kt.startswith("вещественное"):
        return "0.0"
    if kt == "логический":
        return "ложь"
    return "0"


# ─── конвертер ────────────────────────────────────────────────────────────────
class Конвертер:
    def __init__(self, политика=None, исходник_c=None, владение=None, нулевые=None):
        self.нулевые = нулевые or нул.НулевыеУказатели()
        self.тек_исходное_имя = None  # C-имя текущей функции (ключ анализов)
        self.возврат_возможно = False  # текущая функция возвращает «возможно<T*>»
        self.строки = []
        self.строка_узел = []       # параллельно строкам: id узла-владельца строки
        self.строка_функц = []      # параллельно строкам: имя функции-владельца
        self.политика = политика or Политика()
        self.владение = владение or влад.ТаблицаВладения()
        self.ссылочные_имена = set()  # параметры-ссылки текущей функции (изменяемый/вывод)
        self.подстановки = {}       # локальный указатель → узел цели («T *p = &x»)
        self.переименования = {}    # C-имя параметра → Konda-имя (argc→количество_аргументов)
        self.исходник_c = исходник_c or []   # строки исходного .c (для C-ИСХОДНИК)
        self.пометки = []           # список пм.Пометка, попавших в вывод
        self.союзы = set()          # имена union-типов (для детекции доступа к члену)
        self.неизменяемые_глобали = set()  # глобалы, безопасные для «конст»
        # Типизированные слушатели (распознанная C-идиома listener):
        self.сл_типы = {}           # C-структура S → {листтип, T_konda, поля:[(имя_поля, колбэк_узел)]}
        self.сл_экземпляры = {}     # глобал G → {S, поля:[(имя_поля, имя_функции)]}
        self.сл_колбэки = {}        # функция-колбэк → {T_konda, каст_var, каст_id}
        self.сл_рег = {}            # id(CallExpr-регистрации) → (F, объект_узел, G, данные_узел)
        self.поля_структур = {}     # имя структуры → [имена полей] (для { поле = знач })
        self.типы_полей = {}        # имя структуры → [(поле, конда-тип, размер массива)]
        self.срез_переменные = set()  # переменные, ставшие срезом (malloc) — индекс безопасен
        self.ящик_переменные = set()  # переменные-Ящик<T> (одиночный alloc, не убегают)
        self.тек_тело = None          # тело текущей функции (для escape-скана Ящика)
        self.освобождённые_срезы = set()  # имена срезов, у которых уже сняли free
        self.внутри_небезопасно = False  # тело функции целиком обёрнуто в «небезопасно»
        self.тек_узел = None        # id узла-оператора, чьи строки сейчас печатаем
        self.тек_функция = None
        self.функции_с_блокировкой = set()  # там уже есть непереводимое место
        self._счётчик = 0

    @property
    def заметки(self):
        return len(self.пометки)

    def врем_имя(self):
        self._счётчик += 1
        return f"_врем{self._счётчик}"

    def строка_c(self, узел):
        """Исходная строка C для узла (для поля C-ИСХОДНИК в пометке)."""
        н = (узел or {}).get("loc", {}).get("line")
        if isinstance(н, int) and 1 <= н <= len(self.исходник_c):
            return self.исходник_c[н - 1].strip()
        return None

    def добавить_пометку(self, код, узел=None, диагностика=None, деталь=None, ур=0):
        """Печатает блок KONDA-TODO и регистрирует задачу для JSON-отчёта."""
        п = пм.Пометка(код, строка_c=self.строка_c(узел), диагностика=диагностика,
                       деталь=деталь)
        п.строка = len(self.строки) + 1
        self.пометки.append(п)
        if п.категория in пм.БЛОКИРУЮЩИЕ and self.тек_функция:
            # В этой функции есть непереводимое место. Цикл проверки не должен
            # пытаться «чинить» её последствия ослаблением безопасности —
            # ошибки тут вторичны и уйдут вместе с ручной правкой оригинала.
            self.функции_с_блокировкой.add(self.тек_функция)
        self.эмит(ур, п.блок())
        return п

    # ── утилиты обхода ──────────────────────────────────────────────────────────
    @staticmethod
    def развернуть(n):
        while n and n.get("kind") in ("ImplicitCastExpr", "ParenExpr",
                                      "ConstantExpr", "ExprWithCleanups",
                                      "FullExpr"):
            вн = n.get("inner", [])
            if not вн:
                break
            n = вн[-1]
        return n

    @staticmethod
    def имя_ссылки(n):
        rd = n.get("referencedDecl")
        if rd and rd.get("name"):
            return rd["name"]
        return n.get("name", "?")

    def базовое_имя(self, n):
        """СЫРОЕ имя переменной в основании выражения (сквозь касты/индекс/поле).
        Именно сырое: по нему решают, подставлять ли «p» → «x», поэтому само оно
        подстановку применять не должно (иначе «p» уже не найти в таблице)."""
        n = self.развернуть(n)
        if not n:
            return None
        k = n.get("kind")
        if k == "DeclRefExpr":
            return self.имя_ссылки(n)
        вн = n.get("inner", [])
        if k in ("ArraySubscriptExpr", "MemberExpr") and вн:
            return self.базовое_имя(вн[0])
        return None

    # ── детекция «небезопасно» и «непереводимо» ─────────────────────────────────
    def _индекс_на_указателе(self, n):
        """ArraySubscriptExpr на СЫРОМ указателе (не массив-decay, не срез-перем)."""
        вн = n.get("inner", [])
        if not вн:
            return False
        база = вн[0]
        if база.get("kind") == "ImplicitCastExpr" and \
           база.get("castKind") == "ArrayToPointerDecay":
            return False                        # индексируем массив — безопасно
        имя = self.базовое_имя(база)
        if имя in self.срез_переменные or имя in self.ссылочные_имена \
                or имя in self.подстановки:
            return False          # срез / параметр-ссылка / подстановка — безопасно
        # если основание — само значение среза (тип «Срез»/массив) — тоже безопасно;
        # но по qualType этого не видно, поэтому считаем указатель небезопасным
        т = без_квалификаторов(qualtype(база))
        return "*" in т

    def небезопасен(self, n) -> bool:
        """Содержит ли выражение операцию, требующую «небезопасно { }».
        Кроме статических правил учитывает решения цикла проверки (политика):
        узел, на который транспилятор выдал ошибку небезопасности, помечен явно."""
        if not isinstance(n, dict) or "kind" not in n:
            return False
        if n.get("id") in self.политика.небезоп_узлы:
            return True
        k = n["kind"]
        if k == "ArraySubscriptExpr" and self._индекс_на_указателе(n):
            return True
        if k == "UnaryOperator" and n.get("opcode") == "*":
            опнд = self.развернуть(n.get("inner", [{}])[0])
            имя_о = self.базовое_имя(опнд)
            # срез и параметр-ссылка (изменяемый/вывод) разыменовываются безопасно
            if имя_о not in self.срез_переменные and имя_о not in self.ссылочные_имена \
                    and имя_о not in self.подстановки:
                return True
        if k in ("BinaryOperator", "CompoundAssignOperator") and \
           n.get("opcode", "").rstrip("=") in ("+", "-"):
            for c in n.get("inner", []):
                if "*" in без_квалификаторов(qualtype(c)):
                    return True
        if k in ("UnaryOperator",) and n.get("opcode") in ("++", "--"):
            if "*" in без_квалификаторов(qualtype(n)):
                return True
        if k in ("CStyleCastExpr", "ImplicitCastExpr") and \
           n.get("castKind") == "BitCast" and not нул.это_нуль(n):
            # «&x» в типизированную void*-позицию: clang вставляет BitCast
            # T*→void*, но «&» снят (кодоген транспилятора поставит свой), и
            # каст исчезает вместе с ним — реинтерпретации в выводе нет.
            вну = self.развернуть(n.get("inner", [{}])[0])
            if isinstance(вну, dict) and вну.get("kind") == "UnaryOperator" \
                    and вну.get("opcode") == "&" \
                    and вну.get("id") in self.владение.снятые_амперсанды:
                return False
            return True    # reinterpret указателя (но NULL «(void*)0» — не он)
        if k == "MemberExpr":
            база = n.get("inner", [{}])[0]
            if "union " in qualtype(база):
                return True                      # доступ к члену union
        return any(self.небезопасен(c) for c in n.get("inner", []) if isinstance(c, dict))

    def причина_блокировки(self, n):
        """Строка-причина, если выражение содержит непереводимую конструкцию.
        Побитовые операции (& | ^ << >> ~) в Konda ПОДДЕРЖИВАЮТСЯ — не блокируем."""
        if not isinstance(n, dict) or "kind" not in n:
            return None
        if n["kind"] == "UnaryOperator" and n.get("opcode") == "&":
            # «&x», переведённый в аргумент «изменяемый»/«вывод», — не проблема
            if n.get("id") in self.владение.снятые_амперсанды:
                return None
            return ("адрес-оф «&» — в Konda нет; мутация через «изменяемый»/"
                    "«вывод», указатель на локаль недопустим")
        for c in n.get("inner", []):
            r = self.причина_блокировки(c)
            if r:
                return r
        return None

    def заблокирован(self, n) -> bool:
        return self.причина_блокировки(n) is not None

    def _не_бывает_нулём(self, узел):
        """Срез (после «выделить») и параметр-ссылка нулевыми не бывают —
        их null-проверки из C мертвы и сворачиваются в константу."""
        имя = self.базовое_имя(узел)
        return имя in self.срез_переменные or имя in self.ссылочные_имена

    def _истинность(self, узел) -> str:
        """C-скаляр в булевом контексте. Указатель истинен, когда ненулевой:
        clang в режиме C НЕ вставляет PointerToBoolean, поэтому распознаём по
        ТИПУ выражения и разворачиваем в охранник «!= нуль»."""
        т = без_квалификаторов(qualtype(узел))
        if "*" in т and "(" not in т:
            if self._не_бывает_нулём(узел):
                return "истина"
            return f"{self.выражение(узел)} != нуль"
        return self.выражение(узел)

    # ── выражения ───────────────────────────────────────────────────────────────
    def _каст_сужает(self, ист_qt, цель_qt) -> bool:
        """Целочисленный каст ист→цель СУЖАЕТ (цель уже источника)? Только тогда
        нужен явный «как<>()» — расширение/равенство транспилятор принимает сам."""
        wи = _ширина_целого(ист_qt)
        wц = _ширина_целого(цель_qt)
        return wи is not None and wц is not None and wц < wи

    def выражение(self, n) -> str:
        # NULL и истинность указателя распознаются ДО «развернуть»: тот снимает
        # ImplicitCastExpr, а именно в нём (NullToPointer/PointerToBoolean)
        # лежит вся информация.
        if нул.это_нуль(n):
            return "нуль"
        ск = нул._снять_скобки(n)
        if isinstance(ск, dict) and ск.get("kind") == "ImplicitCastExpr" \
                and ск.get("castKind") == "PointerToBoolean" and ск.get("inner"):
            return self._истинность(ск["inner"][0])
        # СУЖАЮЩИЙ неявный каст (size_t→int «int n=strlen(s)», double→int) —
        # «развернуть» его снимает, теряя намерение → транспилятор требует
        # «как<>()» (запрет неявного сужения). Повторяем каст явно (тот же класс
        # потери информации, что и со скобками). Расширяющие касты не трогаем.
        if isinstance(n, dict) and n.get("kind") == "ImplicitCastExpr" \
                and n.get("inner"):
            ck = n.get("castKind")
            if ck == "FloatingToIntegral" or (ck == "IntegralCast"
                    and self._каст_сужает(qualtype(n["inner"][0]), qualtype(n))):
                kt = конда_тип(без_квалификаторов(qualtype(n)))
                return f"как<{kt}>({self.выражение(n['inner'][0])})"
        # ЯВНЫЕ скобки C сохраняем: «развернуть» их снимает, но без них теряется
        # приоритет — «(a+b)/c» стало бы «a+b/c» = «a+(b/c)» (мискомпиляция).
        # Konda использует C-приоритет операторов, поэтому достаточно повторить
        # ровно ту группировку, что дал clang (ParenExpr). Снимаем только касты.
        м = n
        while isinstance(м, dict) and м.get("inner") and м.get("kind") in (
                "ImplicitCastExpr", "ConstantExpr", "ExprWithCleanups", "FullExpr"):
            м = м["inner"][-1]
        if isinstance(м, dict) and м.get("kind") == "ParenExpr" and м.get("inner"):
            return f"({self.выражение(м['inner'][-1])})"
        n = self.развернуть(n)
        if not n or "kind" not in n:
            return ""
        k = n["kind"]
        вн = n.get("inner", [])
        if k == "IntegerLiteral":
            return n.get("value", "0")
        if k == "FloatingLiteral":
            v = n.get("value", "0.0")
            # clang роняет «.0» у целых float («2.0»→«2», «100.0»→«100»). Без
            # точки Konda сочтёт литерал ЦЕЛЫМ → целочисленное деление и тип
            # («(a+b)/2.0» дало бы int-деление). Возвращаем дробную форму.
            if re.fullmatch(r"[+-]?\d+", v):
                v += ".0"
            return v
        if k == "CharacterLiteral":
            v = n.get("value", 0)
            try:
                c = chr(int(v))
                эк = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0",
                      "'": "\\'", "\\": "\\\\"}.get(c, c)
                return f"'{эк}'"
            except (ValueError, TypeError):
                return str(v)
        if k == "StringLiteral":
            return раскодировать_строку(n.get("value", '""'))
        if k == "DeclRefExpr":
            имя_д = self.имя_ссылки(n)
            # «T *p = &x» подставлен: «p» — это просто другое имя для «x»
            if имя_д in self.подстановки:
                return self.выражение(self.подстановки[имя_д])
            # argc/argv точки входа → количество_аргументов/аргументы
            return self.переименования.get(имя_д, имя_д)
        if k == "MemberExpr":
            основа = self.выражение(вн[0]) if вн else ""
            имя_чл = n.get("name", "?")
            # Доступ к анонимному union/struct (clang вставляет неявный MemberExpr
            # с пустым именем) — прозрачен: «m.M.col» = MemberExpr(col) →
            # MemberExpr("") → MemberExpr(M). Без пропуска вышло бы «m.M..col».
            if имя_чл == "":
                return основа
            # p->f и s.f в Konda оба через «.» (кодоген расставит доступ сам)
            return f"{основа}.{имя_чл}"
        if k == "ArraySubscriptExpr":
            # «p[0]» для параметра-ссылки (изменяемый/вывод) — это сам объект:
            # кодоген Konda разыменует его сам.
            имя_б = self.базовое_имя(вн[0])
            if (имя_б in self.ссылочные_имена or имя_б in self.подстановки) \
                    and self.выражение(вн[1]) == "0":
                return self.выражение(вн[0])
            return f"{self.выражение(вн[0])}[{self.выражение(вн[1])}]"
        if k == "BinaryOperator":
            оп = n.get("opcode", "?")
            оп = {"&&": "и", "||": "или"}.get(оп, оп)
            # Мёртвая NULL-проверка (срез после malloc / параметр-ссылка):
            # такие значения нулевыми не бывают → константа.
            if оп in ("==", "!=") and len(вн) >= 2:
                нулевой = [нул.это_нуль(c) for c in вн[:2]]
                if any(нулевой):
                    другой = вн[1] if нулевой[0] else вн[0]
                    if self._не_бывает_нулём(другой):
                        return "ложь" if оп == "==" else "истина"
            # операнды «и»/«или» — булев контекст (указатель → «!= нуль»)
            if оп in ("и", "или"):
                return (f"{self._истинность(вн[0])} {оп} "
                        f"{self._истинность(вн[1])}")
            return f"{self.выражение(вн[0])} {оп} {self.выражение(вн[1])}"
        if k == "CompoundAssignOperator":
            л, п = self.выражение(вн[0]), self.выражение(вн[1])
            оп = n.get("opcode", "+=")[:-1]
            return f"{л} = {л} {оп} {п}"
        if k == "UnaryOperator":
            оп = n.get("opcode", "?")
            # «!p» на указателе — охранник «п == нуль» (форма, которую понимает
            # разворот «возможно»); на срезе/ссылке — мёртвая проверка → «ложь».
            # PointerToBoolean clang в режиме C не ставит — узнаём по типу.
            if оп == "!" and вн:
                т_опнд = без_квалификаторов(qualtype(вн[0]))
                if "*" in т_опнд and "(" not in т_опнд:
                    if self._не_бывает_нулём(вн[0]):
                        return "ложь"
                    return f"{self.выражение(вн[0])} == нуль"
                # Konda: унарного «!» нет. Для БУЛЕВА операнда (сравнение/логика/
                # вложенный «!») — «(… == ложь)»; для ЦЕЛОГО — «(… == 0)».
                вну = self.развернуть(вн[0])
                булев = (вну.get("kind") == "BinaryOperator"
                         and вну.get("opcode") in ("==", "!=", "<", ">", "<=",
                                                   ">=", "&&", "||")) \
                    or (вну.get("kind") == "UnaryOperator"
                        and вну.get("opcode") == "!")
                хвост = "ложь" if булев else "0"
                return f"({self.выражение(вн[0])} == {хвост})"
            опнд = self.выражение(вн[0])
            if оп in ("++", "--"):
                зн = "+" if оп == "++" else "-"
                return f"{опнд} = {опнд} {зн} 1"
            if оп == "*":
                имя_о = self.базовое_имя(вн[0])
                # «*p» подставленного указателя и параметра-ссылки — сам объект
                if имя_о in self.подстановки or имя_о in self.ссылочные_имена:
                    return опнд
                return f"{опнд}[0]"              # разыменование → [0]
            if оп == "&":
                # «&x» в аргументе для «изменяемый»/«вывод» просто исчезает —
                # кодоген Konda поставит «&» сам.
                if n.get("id") in self.владение.снятые_амперсанды:
                    return опнд
                return f"/*&*/{опнд}"            # помечено; заблокирован() отловит
            if оп in ("-", "+", "!", "~"):
                return f"{оп}{опнд}"
            return f"{оп}{опнд}"
        if k == "CallExpr":
            callee = self.выражение(вн[0]) if вн else "?"
            арги = ", ".join(self.выражение(a) for a in вн[1:])
            return f"{callee}({арги})"
        if k in ("CStyleCastExpr", "CXXStaticCastExpr"):
            цель = конда_тип(qualtype(n))
            внутр = self.выражение(вн[0]) if вн else ""
            return f"как<{цель}>({внутр})"
        if k == "ConditionalOperator":
            return (f"/*тернарник — вынесите в если/иначе*/ "
                    f"({self.выражение(вн[0])} ? {self.выражение(вн[1])} "
                    f": {self.выражение(вн[2])})")
        if k == "UnaryExprOrTypeTraitExpr":       # sizeof
            арг = n.get("argType", {})
            if isinstance(арг, dict) and арг.get("qualType"):
                return f"размер_обьекта({конда_тип(арг['qualType'])})"
            if вн:
                return f"размер_обьекта({self.выражение(вн[0])})"
            return "размер_обьекта(целое32)"
        if k == "InitListExpr":
            return self._инициализатор(n)
        if k == "ImplicitValueInitExpr":
            return "0"
        if k in ("CompoundLiteralExpr",):
            return self.выражение(вн[-1]) if вн else "{ }"
        return f"/*?{k}*/"

    def _инициализатор(self, n) -> str:
        """InitListExpr → «{ поле = знач }» для структуры, «{ a, b }» для массива."""
        т = без_квалификаторов(qualtype(n))
        вн = [c for c in n.get("inner", []) if isinstance(c, dict) and "kind" in c]
        имя_структуры = None
        if т.startswith("struct "):
            имя_структуры = т[len("struct "):].strip()
        elif т in self.поля_структур:
            имя_структуры = т
        поля = self.поля_структур.get(имя_структуры)
        if поля:
            части = []
            for i, c in enumerate(вн):
                if i < len(поля):
                    части.append(f"{поля[i]} = {self.выражение(c)}")
                else:
                    части.append(self.выражение(c))
            return "{ " + ", ".join(части) + " }"
        # массив / неизвестная структура → позиционно
        return "{ " + ", ".join(self.выражение(c) for c in вн) + " }"

    # ── операторы ───────────────────────────────────────────────────────────────
    def эмит(self, ур, текст):
        for строка in текст.split("\n"):
            self._строка(("    " * ур + строка) if строка else "")

    def _строка(self, текст):
        """Единственная точка записи строки — попутно запоминает узла-владельца
        и функцию, чтобы диагностику «строка N» вернуть к решению в AST."""
        self.строки.append(текст)
        self.строка_узел.append(self.тек_узел)
        self.строка_функц.append(self.тек_функция)

    def узел_на_строке(self, н):
        """id узла, породившего строку н (1-индексная), либо None."""
        i = н - 1
        return self.строка_узел[i] if 0 <= i < len(self.строка_узел) else None

    def функция_на_строке(self, н):
        i = н - 1
        return self.строка_функц[i] if 0 <= i < len(self.строка_функц) else None

    def оператор(self, n, ур):
        if not isinstance(n, dict) or "kind" not in n:
            return
        # Владелец печатаемых далее строк — этот оператор. Так диагностика
        # «строка N» из транспилятора возвращается к решению в AST.
        предыдущий = self.тек_узел
        if n.get("id"):
            self.тек_узел = n["id"]
        try:
            # Цикл проверки мог сдаться на этом узле — печатаем его пометку.
            метка = self.политика.пометки_узлы.get(n.get("id"))
            if метка:
                код, деталь, диаг = метка
                self.добавить_пометку(код, n, диагностика=диаг, деталь=деталь, ур=ур)
            self._оператор(n, ур)
        finally:
            self.тек_узел = предыдущий

    def _оператор(self, n, ур):
        k = n["kind"]
        вн = n.get("inner", [])

        if k == "CompoundStmt":
            for c in вн:
                self.оператор(c, ур)
            return
        if k == "NullStmt":
            return
        if k == "DeclStmt":
            for d in вн:
                self.объявление(d, ур)
            return
        if k == "ReturnStmt":
            self.возврат(вн[0] if вн else None, ур)
            return
        if k == "IfStmt":
            self.если(n, ур)
            return
        if k == "WhileStmt":
            self.цикл_пока(n, ур)
            return
        if k == "DoStmt":
            self.цикл_делай(n, ур)
            return
        if k == "ForStmt":
            self.цикл_для(n, ур)
            return
        if k == "SwitchStmt":
            self.выбор_в_если(n, ур)
            return
        if k == "BreakStmt":
            self.эмит(ур, "прервать")
            return
        if k == "ContinueStmt":
            self.эмит(ур, "продолжить")
            return
        if k == "GotoStmt":
            gc = self.goto_cleanup
            if gc and n.get("targetLabelDeclId") == gc["declid"]:
                аргс = ", ".join(имя for имя, _ in gc["параметры"])
                вызов = f"{gc['helper']}({аргс})"
                if gc["возвр_ничего"]:
                    self.эмит(ур, вызов)
                    self.эмит(ур, "вернуть")
                else:
                    self.эмит(ур, f"вернуть {вызов}")
                return
            self.добавить_пометку("goto", n, ур=ур)
            return
        if k in ("LabelStmt",):
            self.добавить_пометку("goto", n, деталь=f"метка «{n.get('name','?')}»", ур=ур)
            for c in вн:
                self.оператор(c, ур)
            return
        # выражение-оператор (вызов/присваивание/инкремент)
        self.оператор_выражение(n, ур)

    def _обнулить_поля(self, цель, имя_структуры, ур):
        """«memset(&s, 0, sizeof(s))» → присваивания полям. → 1/0 (смог ли).
        Отказывается при поле-указателе (нулевой указатель в Konda — только
        «возможно», а memset делал бы его молча) и неизвестной вложенности."""
        типы = self.типы_полей.get(имя_структуры)
        if типы is None:
            return 0
        строки = []
        for имя_п, kt, разм in типы:
            if kt.endswith("*"):
                return 0                      # поле-указатель: не выразить нулём
            if kt in self.типы_полей:         # вложенная структура — рекурсивно
                вложенные = []
                if not self._обнулить_поля_в(f"{цель}.{имя_п}", kt, вложенные):
                    return 0
                строки.extend(вложенные)
                continue
            умолч = значение_по_умолчанию(kt)
            if умолч is None:
                return 0
            if разм is not None:
                и = self.врем_имя()
                строки.append(f"для целое32 {и} = 0; {и} < {разм}; "
                              f"{и} = {и} + 1 {{ {цель}.{имя_п}[{и}] = {умолч} }}")
            else:
                строки.append(f"{цель}.{имя_п} = {умолч}")
        for с in строки:
            self.эмит(ур, с)
        return 1

    def _обнулить_поля_в(self, цель, имя_структуры, строки):
        """Как _обнулить_поля, но собирает строки (для вложенных структур)."""
        типы = self.типы_полей.get(имя_структуры)
        if типы is None:
            return 0
        for имя_п, kt, разм in типы:
            if kt.endswith("*") or разм is not None:
                return 0
            if kt in self.типы_полей:
                if not self._обнулить_поля_в(f"{цель}.{имя_п}", kt, строки):
                    return 0
                continue
            умолч = значение_по_умолчанию(kt)
            if умолч is None:
                return 0
            строки.append(f"{цель}.{имя_п} = {умолч}")
        return 1

    def _эмит_оператор_строку(self, стр, n, ур):
        """Эмит строки-оператора с обёрткой «небезопасно { }», если выражение
        небезопасно и мы ещё не внутри неё."""
        if self.небезопасен(n) and not self.внутри_небезопасно:
            self.эмит(ур, "небезопасно { " + стр + " }")
        else:
            self.эмит(ур, стр)

    def оператор_выражение(self, n, ур):
        # free(срез) отбрасываем — у среза автоосвобождение (autofree)
        внр = self.развернуть(n)
        # Регистрация слушателя: X_add_listener(obj, &G, data) → слушать(...).
        рег = self.сл_рег.get(внр.get("id"))
        if рег is not None:
            F, объект, G, данные = рег
            self.эмит(ур, f"слушать({F}, {self.выражение(объект)}, {G}, "
                          f"{self.выражение(данные)})")
            return
        if внр.get("kind") == "CallExpr":
            дети = внр.get("inner", [])
            if дети and self.базовое_имя(дети[0]) == "free" and len(дети) == 2:
                # ВАЖНО: базовое_имя() спускается через MemberExpr/ArraySubscript,
                # поэтому «free(w->buffers)» тоже давало бы «w» — это ложный
                # double-free (освобождаем ПОЛЕ, не саму переменную). Для
                # детектора берём СТРОГИЙ корень: free(x) — да, free(x->…) /
                # free(x[…]) — нет.
                арг = self.развернуть(дети[1])
                строгое_имя = (self.имя_ссылки(арг)
                               if арг and арг.get("kind") == "DeclRefExpr"
                               else None)
                имя_осв = self.базовое_имя(дети[1])
                # free(Ящик) снимается: у Ящика автоosвобождение при выходе.
                # (escape-гейт _ящик_локальна уже гарантировал, что кроме free и
                # доступа к полю переменная нигде не используется.)
                if строгое_имя and строгое_имя in self.ящик_переменные:
                    return
                if строгое_имя and строгое_имя in self.срез_переменные:
                    # Освобождение снимаем (autofree). Но если ту же переменную
                    # уже освобождали в этой функции — в C это double-free.
                    # Перевод безопасен, а исходник — нет: помечаем ПРОВЕРИТЬ.
                    if строгое_имя in self.освобождённые_срезы:
                        self.добавить_пометку("двойной-free", n,
                                              деталь=f"переменная «{строгое_имя}»",
                                              ур=ур)
                    else:
                        self.освобождённые_срезы.add(строгое_имя)
                    return
                if имя_осв in self.срез_переменные:
                    # free(x->field) / free(x[i]) — снимаем без пометки:
                    # это освобождение ПРОЕКЦИИ, не всей переменной.
                    return
            # «memset(&s, 0, sizeof(s))» — идиома обнуления структуры: сырой
            # «&» не нужен, обнуляем полями (для массивов — циклом). Статически
            # эквивалентно и безопасно; при поле-указателе — обычная пометка.
            if дети and self.базовое_имя(дети[0]) == "memset" and len(дети) == 4:
                адрес = влад.адрес_lvalue(дети[1])
                разм = self.развернуть(дети[3])
                if адрес is not None and нул._целочисленный_нуль(дети[2]) \
                        and разм.get("kind") == "UnaryExprOrTypeTraitExpr":
                    внутри = self.развернуть((адрес.get("inner") or [{}])[0])
                    т = без_квалификаторов(qualtype(внутри))
                    имя_с = т[len("struct "):].strip() if т.startswith("struct ") \
                        else т
                    цель = self.выражение(внутри)
                    if self._обнулить_поля(цель, имя_с, ур):
                        return
        причина = self.причина_блокировки(n)
        if причина:
            self.добавить_пометку("адрес-оф", n, деталь="в выражении-операторе", ур=ур)
            self.эмит(ур, "// " + self.выражение(n))
            return
        разв = self.развернуть(n)
        k_разв = разв.get("kind")
        # Составное присваивание «x += y» → НАТИВНОЕ (транспилятор §45), а не
        # разворот «x = x + y»: цель эмитится ОДИН раз (иначе побочка в цели
        # сработала бы дважды), узкие типы не расширяются, работают guard'ы/
        # перегрузка операторов транспилятора.
        if k_разв == "CompoundAssignOperator":
            вн2 = разв.get("inner", [])
            if len(вн2) >= 2:
                оп = разв.get("opcode", "+=")
                self._эмит_оператор_строку(
                    f"{self.выражение(вн2[0])} {оп} {self.выражение(вн2[1])}", n, ур)
                return
        # Постфиксный «x++»/«x--» как ОПЕРАТОР → нативный (§47): C гарантирует
        # single-eval, узкий тип не расширяется (в отличие от «x = x + 1»).
        if k_разв == "UnaryOperator" and разв.get("opcode") in ("++", "--"):
            вн2 = разв.get("inner", [])
            if вн2:
                self._эмит_оператор_строку(
                    f"{self.выражение(вн2[0])}{разв.get('opcode')}", n, ур)
                return
        # «x = cond ? a : b» (присваивание тернарника) → если/иначе (в Konda нет
        # тернарника-выражения; return/объявление с тернарником уже так делают).
        if k_разв == "BinaryOperator" and разв.get("opcode") == "=":
            вн2 = разв.get("inner", [])
            прав = self.развернуть(вн2[1]) if len(вн2) >= 2 else {}
            if прав.get("kind") == "ConditionalOperator":
                # ветви могут быть вложенными тернарниками → рекурсивно
                self._присвоить_знач(self.выражение(вн2[0]), вн2[1], ур)
                return
        # цепное присваивание «a = b = 0» → последовательность справа налево
        цепь = self.развернуть(n)
        if цепь.get("kind") == "BinaryOperator" and цепь.get("opcode") == "=":
            цели, тек = [], цепь
            while тек.get("kind") == "BinaryOperator" and тек.get("opcode") == "=":
                цели.append(тек.get("inner", [{}])[0])
                тек = self.развернуть(тек.get("inner", [{}, {}])[1])
            if len(цели) > 1:
                значение = self.выражение(тек)
                for цель in reversed(цели):
                    self.эмит(ур, f"{self.выражение(цель)} = {значение}")
                    значение = self.выражение(цель)
                return
        строка = self.выражение(n)
        if self.небезопасен(n) and not self.внутри_небезопасно:
            self.эмит(ур, "небезопасно { " + строка + " }")
        else:
            self.эмит(ур, строка)

    def объявление(self, d, ур, префикс=""):
        if d.get("kind") == "RecordDecl":       # вложенная struct/union — верхнеуровнево
            return
        if d.get("id") in self.владение.пропустить_объявления:
            return          # «T *p = &x» — вместо «p» печатаем «x» (подстановка)
        if d.get("id") in getattr(self, "сл_подавить", ()):
            return          # «struct T *cv = data» в колбэке слушателя — cv стал параметром
        if d.get("kind") == "StaticAssertDecl":
            return          # «_Static_assert» — проверка времени компиляции C;
                            # рантайм-эффекта нет, аналога в Konda нет — молча снимаем
        if d.get("kind") == "EnumDecl":
            # Локальный enum (объявлен в теле функции) — тип верхнего уровня в
            # Konda. Уже вынесен в top-level декларации пред-проходом
            # (собрать_локальные_enum), здесь только не печатаем повторно.
            return
        if d.get("kind") != "VarDecl":
            self.добавить_пометку("проверка-транспилятора", d,
                                 деталь=f"объявление {d.get('kind')} пропущено", ур=ур)
            return
        имя = d.get("name", "_")
        qt = qualtype(d)
        # Локаль-указатель на функцию «RET (*p)(ARGS)»: конда_тип даёт битый
        # синтаксис (нет встроенного fnptr-типа). Честная пометка вместо мусора —
        # для локали нужен именованный «типфункции» на верхнем уровне (у ПАРАМЕТРОВ
        # конвертер синтезирует его сам; локальная переменная — отдельный случай).
        if _разобрать_фнптр(qt) is not None:
            self.добавить_пометку("указатель-функции-локаль", d, ур=ур)
            дети = [c for c in d.get("inner", [])
                    if isinstance(c, dict) and "kind" in c]
            self.эмит(ур, f"// {имя} = {self.выражение(дети[-1])}" if дети
                          else f"// {имя}")
            return
        kt = конда_тип(qt)
        вн = [c for c in d.get("inner", []) if isinstance(c, dict) and "kind" in c]
        иниц = вн[-1] if вн else None
        массив = "[" in без_квалификаторов(qt)
        разм = ""
        if массив:
            # Многомерный массив (int a[N][M]) — в языке пока нет; НЕ эмитим «a[N]»
            # молча (потеря 2-го измерения = тихая мискомпиляция). Явная пометка.
            if без_квалификаторов(qt).count("[") > 1:
                self.добавить_пометку("многомерный-массив", d, ур=ур)
                self.эмит(ур, f"// {конда_тип(без_квалификаторов(qt).split('[')[0].strip())} "
                              f"{имя}{без_квалификаторов(qt)[len(без_квалификаторов(qt).split('[')[0]):]}")
                return
            m = re.search(r"\[(\d*)\]", без_квалификаторов(qt))
            разм = m.group(1) if m else ""
            баз = без_квалификаторов(qt).split("[")[0].strip()
            kt = конда_тип(баз)

        # Одиночная аллокация структуры, не убегающая → Ящик<T> (autofree free).
        ящик = self._alloc_в_ящик(имя, qt, иниц)
        if ящик is not None:
            self.ящик_переменные.add(имя)
            self.эмит(ур, ящик)
            return
        # malloc/calloc/zalloc(N*sizeof) → срез<T> имя = выделить(N)
        срез = self._malloc_в_срез(имя, qt, иниц)
        if срез is not None:
            self.срез_переменные.add(имя)
            self.эмит(ур, срез)
            return

        нулевая = (kt.endswith("*") and not массив
                   and self.нулевые.нулевой(self.тек_исходное_имя, имя))
        объ = (f"{префикс}возможно<{kt}> {имя}" if нулевая
               else f"{префикс}{kt} {имя}")
        if массив:
            объ += f"[{разм}]"
        if иниц is None:
            # nullable без инициализатора в C — «пока пусто»; в Konda без
            # инициализатора нельзя, а «нуль» тут ровно то, что имел в виду C
            self.эмит(ур, f"{объ} = нуль" if нулевая else объ)
            return
        # char-массив со СТРОКОВЫМ инициализатором: «char s[16] = "hi"» →
        # «символ s[16] = { 'h', 'i', '\0' }» (транспилятор требует литерал
        # массива; хвост C зануляет). Размер, если C не указал (char s[]="hi"),
        # берём как длина строки + NUL. char-литералы (§31) делают это выразимым.
        if массив and kt == "символ" \
                and self.развернуть(иниц).get("kind") == "StringLiteral":
            байты = _строка_литерал_в_байты(self.развернуть(иниц).get("value", '""'))
            элементы = [_байт_в_символ(b) for b in байты] + ["'\\0'"]
            разм_л = разм if разм else str(len(байты) + 1)
            self.эмит(ур, f"{префикс}{kt} {имя}[{разм_л}] = {{ "
                          + ", ".join(элементы) + " }")
            return
        причина = self.причина_блокировки(иниц)
        if причина:
            self.добавить_пометку("адрес-оф", d, деталь="в инициализаторе объявления", ур=ур)
            self.эмит(ур, f"// {объ} = {self.выражение(иниц)}")
            return
        # внутри «небезопасно { }» (вся функция обёрнута) объявляем как есть
        if self.внутри_небезопасно:
            self.эмит(ур, f"{объ} = {self.выражение(иниц)}")
            return
        if иниц.get("kind") == "ConditionalOperator":
            self._тернарник_в_если(объ, имя, иниц, ур)
            return
        if self.небезопасен(иниц):
            умолч = значение_по_умолчанию(kt if not массив else "нет")
            if массив or умолч is None:
                # указатель/массив с небезопасным инициализатором — обернуть целиком
                self.эмит(ур, "небезопасно { " + объ + " = " + self.выражение(иниц) + " }")
            else:
                self.эмит(ур, f"{объ} = {умолч}")
                self.эмит(ур, "небезопасно { " + f"{имя} = {self.выражение(иниц)}" + " }")
            return
        self.эмит(ур, f"{объ} = {self.выражение(иниц)}")

    # Аллокаторы-обёртки, эквивалентные malloc/calloc по смыслу (возвращают
    # свежий буфер, который потом free). weston повсеместно использует zalloc
    # (calloc(1, n)) и x*alloc (обёртки с проверкой OOM). Массивные их формы
    # (n*sizeof / calloc(n,s)) — законные срезы; одиночные (bare sizeof) — нет
    # (это владеющий одиночный объект, целевая конструкция — Ящик<T>, не срез;
    # срез<T> длины 1 сломал бы доступ «.поле»).
    _АЛЛОКАТОРЫ = ("malloc", "calloc", "zalloc", "xmalloc", "xzalloc", "xcalloc")

    def _разбор_аллокации(self, узел_вызова, qt):
        """Вызов-аллокатор + тип указателя → (элем_konda, счёт, одиночный).
        Иначе None. Одиночный=True для «alloc(sizeof(T))» (не массив)."""
        узел = self.развернуть(узел_вызова)
        if узел.get("kind") != "CallExpr":
            return None
        вн = узел.get("inner", [])
        if not вн:
            return None
        имя_ф = self.базовое_имя(вн[0]) or ""
        if имя_ф not in self._АЛЛОКАТОРЫ:
            return None
        т = без_квалификаторов(qt)
        if "*" not in т:
            return None
        элем = конда_тип(т[:т.rindex("*")].strip())
        # zalloc/xzalloc — один аргумент-размер (как malloc); calloc-семейство с
        # двумя аргументами трактуем как calloc(n, s).
        имя_счёта = "calloc" if имя_ф in ("calloc", "xcalloc") else "malloc"
        счёт = self._счёт_из_malloc(имя_счёта, вн[1:])
        return (элем, счёт, счёт == "1")

    def _malloc_в_срез(self, имя, qt, иниц):
        """T* p = (T*)malloc(N*sizeof(T)) → срез<T> p = выделить(N). Иначе None."""
        if иниц is None:
            return None
        разбор = self._разбор_аллокации(иниц, qt)
        if разбор is None:
            return None
        элем, счёт, одиночный = разбор
        имя_ф = self.базовое_имя(self.развернуть(иниц).get("inner", [{}])[0]) or ""
        # Одиночная аллокация СТРУКТУРЫ — не срез, а Ящик<T> (см. _alloc_в_ящик):
        # срез<T> длины 1 сломал бы «.поле».
        if одиночный and self._известная_структура(элем):
            return None
        # Обёртки-аллокаторы (zalloc/x*) добавляем только для МАССИВНЫХ форм:
        # их одиночная форма (alloc(sizeof(примитив))) — владеющий одиночный
        # объект; для примитива Ящик v1 не годится, оставляем сырым.
        # malloc/calloc сохраняют прежнее поведение (в т.ч. одиночный примитив).
        if имя_ф not in ("malloc", "calloc") and одиночный:
            return None
        return f"срез<{элем}> {имя} = выделить({счёт})"

    def _известная_структура(self, элем):
        """Konda-тип «элем» — известная структура проекта (её устройство и sizeof
        доступны транспилятору → можно завести Ящик<элем>)."""
        return элем in self.поля_структур

    def _alloc_в_ящик(self, имя, qt, иниц):
        """T* p = alloc(sizeof(T)) (ОДИНОЧНАЯ структура), причём p не убегает
        (используется только как база «p->поле» и в free(p)) → «Ящик<T> p =
        выделить()». Иначе None. Escape-гейт: если p передаётся в функцию,
        присваивается, возвращается или берётся адрес — Ящик сломал бы типы
        (Ящик<T> ≠ T*), поэтому оставляем как есть."""
        if иниц is None or self.тек_тело is None:
            return None
        разбор = self._разбор_аллокации(иниц, qt)
        if разбор is None:
            return None
        элем, _счёт, одиночный = разбор
        if not одиночный or not self._известная_структура(элем):
            return None
        if not self._ящик_локальна(имя, self.тек_тело):
            return None
        return f"Ящик<{элем}> {имя} = выделить()"

    def _ящик_локальна(self, имя, n, безопасно=False):
        """True, если каждое использование «имя» — безопасная позиция для Ящика:
        база доступа к полю (p->f / p.f) или аргумент free(p). Любое другое
        (аргумент вызова, присваивание, возврат, «&p», индексация) = escape."""
        if not isinstance(n, dict) or "kind" not in n:
            return True
        k = n.get("kind")
        if k in ("ImplicitCastExpr", "CStyleCastExpr", "ParenExpr",
                 "ExprWithCleanups"):
            # Прозрачные обёртки (lvalue→rvalue, decay, скобки) — позиция та же.
            return all(self._ящик_локальна(имя, c, безопасно)
                       for c in n.get("inner", []))
        if k == "UnaryExprOrTypeTraitExpr":
            return True   # sizeof/alignof — операнд НЕ вычисляется («sizeof *o»)
        if k == "DeclRefExpr":
            if self.имя_ссылки(n) == имя:
                return безопасно
            return True
        if k == "MemberExpr":
            # база (первый ребёнок) — безопасная позиция; остальные — как обычно
            вн = n.get("inner", [])
            return all(self._ящик_локальна(имя, c, безопасно=(i == 0))
                       for i, c in enumerate(вн))
        if k == "CallExpr":
            вн = n.get("inner", [])
            if вн and self.базовое_имя(вн[0]) == "free":
                return True   # free(p) снимется автоosвобождением — не escape
            return all(self._ящик_локальна(имя, c, False) for c in вн)
        return all(self._ящик_локальна(имя, c, False) for c in n.get("inner", []))

    def _счёт_из_malloc(self, имя_ф, аргс):
        """Оценивает число элементов для выделить(): calloc(n,s)→n; n*sizeof→n."""
        if имя_ф == "calloc" and len(аргс) == 2:
            return self.выражение(аргс[0])
        if len(аргс) == 1:
            a = self.развернуть(аргс[0])
            if a.get("kind") == "BinaryOperator" and a.get("opcode") == "*":
                л, п = a.get("inner", [])
                лr, пr = self.развернуть(л), self.развернуть(п)
                if пr.get("kind") == "UnaryExprOrTypeTraitExpr":
                    return self.выражение(л)
                if лr.get("kind") == "UnaryExprOrTypeTraitExpr":
                    return self.выражение(п)
            if a.get("kind") == "UnaryExprOrTypeTraitExpr":
                return "1"
            return self.выражение(аргс[0]) + " /* TODO(konda): проверьте число элементов */"
        return "0"

    def _присвоить_знач(self, цель, выр, ур):
        """Эмит «цель = выр»; если выр — тернарник, разворачивает в если/иначе
        РЕКУРСИВНО (вложенный «c1 ? (c2?a:b) : d» → вложенные если). В Konda нет
        тернарника-выражения."""
        разв = self.развернуть(выр)
        if разв.get("kind") == "ConditionalOperator":
            вн = разв.get("inner", [])
            self.эмит(ур, f"если ({self.выражение(вн[0])}) {{")
            self._присвоить_знач(цель, вн[1], ур + 1)
            self.эмит(ур, "} иначе {")
            self._присвоить_знач(цель, вн[2], ур + 1)
            self.эмит(ур, "}")
        else:
            self.эмит(ур, f"{цель} = {self.выражение(выр)}")

    def _вернуть_знач(self, выр, ур):
        """Эмит «вернуть выр»; тернарник → если/иначе рекурсивно."""
        разв = self.развернуть(выр)
        if разв.get("kind") == "ConditionalOperator":
            вн = разв.get("inner", [])
            self.эмит(ур, f"если ({self.выражение(вн[0])}) {{")
            self._вернуть_знач(вн[1], ур + 1)
            self.эмит(ур, "} иначе {")
            self._вернуть_знач(вн[2], ур + 1)
            self.эмит(ур, "}")
        else:
            self.эмит(ур, f"вернуть {self.выражение(выр)}")

    def _тернарник_в_если(self, объ, имя, терн, ур):
        вн = терн.get("inner", [])
        self.эмит(ур, объ)
        self.эмит(ур, f"если ({self.выражение(вн[0])}) {{")
        self._присвоить_знач(имя, вн[1], ур + 1)   # ветвь может быть тернарником
        self.эмит(ур, "} иначе {")
        self._присвоить_знач(имя, вн[2], ур + 1)
        self.эмит(ур, "}")

    def возврат(self, выр, ур):
        if выр is None:
            self.эмит(ур, "вернуть")
            return
        причина = self.причина_блокировки(выр)
        if причина:
            self.добавить_пометку("адрес-оф", выр, деталь="в выражении «вернуть»", ур=ур)
            self.эмит(ур, f"// вернуть {self.выражение(выр)}")
            умолч = ("нуль" if self.возврат_возможно
                     else значение_по_умолчанию(self.тип_возврата))
            self.эмит(ур, f"вернуть {умолч if умолч else '0'}")
            return
        внр = self.развернуть(выр)
        if внр.get("kind") == "ConditionalOperator":
            self._вернуть_знач(выр, ур)     # ветви могут быть вложенными тернарниками
            return
        if self.небезопасен(выр) and not self.внутри_небезопасно:
            умолч = значение_по_умолчанию(self.тип_возврата)
            if умолч is None:
                # возврат указателя из небезопасного выражения — вся функция обёрнута
                self.эмит(ур, f"вернуть {self.выражение(выр)}")
            else:
                t = self.врем_имя()
                self.эмит(ур, f"{self.тип_возврата} {t} = {умолч}")
                self.эмит(ур, "небезопасно { " + f"{t} = {self.выражение(выр)}" + " }")
                self.эмит(ур, f"вернуть {t}")
            return
        self.эмит(ур, f"вернуть {self.выражение(выр)}")

    def _условие(self, узел, ур):
        """Возвращает строку-условие; при небезопасности выносит в temp и
        возвращает имя temp (для if). Для циклов небезопасное условие обёрнуто."""
        if self.небезопасен(узел) or self.заблокирован(узел):
            t = self.врем_имя()
            self.эмит(ур, f"логический {t} = ложь")
            нужна = self.небезопасен(узел) and not self.внутри_небезопасно
            обёртка = "небезопасно { " if нужна else ""
            закр = " }" if обёртка else ""
            if self.заблокирован(узел):
                self.добавить_пометку("адрес-оф", узел, деталь="в условии", ур=ур)
            self.эмит(ур, f"{обёртка}{t} = ({self.выражение(узел)}){закр}")
            return t
        return self._истинность(узел)

    def если(self, n, ур):
        вн = n.get("inner", [])
        усл = self._условие(вн[0], ур)
        self.эмит(ур, f"если ({усл}) {{")
        self.тело(вн[1], ур)
        # Цепочка «иначе если»: пока ветвь «иначе» — это IfStmt с БЕЗОПАСНЫМ
        # условием, печатаем «} иначе если (…) {» на том же уровне (плоско, как в
        # исходном C). Небезопасное/заблокированное условие требует temp ПЕРЕД
        # проверкой (_условие его эмитит), а на уровне «иначе если» temp
        # вычислялся бы всегда, до входа в ветвь — поэтому там уходим во
        # ВЛОЖЕННУЮ форму «} иначе { если … }» (temp внутри блока «иначе»).
        while len(вн) > 2 and isinstance(вн[2], dict) and вн[2].get("kind") == "IfStmt":
            вложенный = вн[2]
            внв = вложенный.get("inner", [])
            усл_узел = внв[0] if внв else {}
            if self.небезопасен(усл_узел) or self.заблокирован(усл_узел):
                self.эмит(ур, "} иначе {")
                self.если(вложенный, ур + 1)
                self.эмит(ур, "}")
                return
            усл2 = self._условие(усл_узел, ур)   # безопасно → без temp, выражение
            self.эмит(ур, f"}} иначе если ({усл2}) {{")
            self.тело(внв[1], ур)
            вн = внв                              # продолжаем по ветви «иначе»
        if len(вн) > 2 and isinstance(вн[2], dict) and "kind" in вн[2]:
            self.эмит(ур, "} иначе {")
            self.тело(вн[2], ур)
        self.эмит(ур, "}")

    def _есть_присваивание(self, m):
        """Есть ли в выражении присваивание (простое «=» или составное «+=»…)."""
        if not isinstance(m, dict) or "kind" not in m:
            return False
        раз = self.развернуть(m)
        k = раз.get("kind")
        if k == "CompoundAssignOperator" \
                or (k == "BinaryOperator" and раз.get("opcode") == "="):
            return True
        return any(self._есть_присваивание(c) for c in раз.get("inner", [])
                   if isinstance(c, dict))

    def _тело_имеет_continue(self, node):
        """«continue» в теле, принадлежащий ИМЕННО этому циклу (во вложенные
        циклы не спускаемся — там continue относится к ним)."""
        if not isinstance(node, dict) or "kind" not in node:
            return False
        k = node.get("kind")
        if k == "ContinueStmt":
            return True
        if k in ("ForStmt", "WhileStmt", "DoStmt"):
            return False
        return any(self._тело_имеет_continue(c) for c in node.get("inner", [])
                   if isinstance(c, dict))

    def _вынести_присваивания_условия(self, узел):
        """C-идиома «присваивание встроено в условие» (while ((x=f())!=NULL),
        n = read(...) > 0 …). В Konda присваивание — ОПЕРАТОР, не выражение,
        поэтому такое условие напрямую не транслируется: его надо вынести
        операторами ПЕРЕД проверкой.

        → (присваивания, условие2, безопасно):
          • присваивания — узлы «=»/«+=»… в порядке ВЫЧИСЛЕНИЯ (слева направо,
            вложенное «a=b=f()» — b=f() раньше a=b); каждый эмитится оператором
            до проверки; пусто → присваиваний не было;
          • условие2 — КОПИЯ условия, где каждое присваивание заменено на свою
            цель (LHS): «(x = f()) != нуль» → «x != нуль»;
          • безопасно=False, если присваивание стоит в позиции УСЛОВНОГО
            вычисления (правый операнд «&&»/«||», ветви «?:») — безусловный вынос
            сломал бы короткое замыкание; вызывающий тогда помечает, не переписывает.
        Если присваиваний нет — ([], узел, True) (без копирования)."""
        if not self._есть_присваивание(узел):
            return [], узел, True

        присваивания = []
        небезопасно_замыкание = [False]

        def присваивание(m):
            k = self.развернуть(m).get("kind")
            оп = self.развернуть(m).get("opcode")
            return k == "CompoundAssignOperator" \
                or (k == "BinaryOperator" and оп == "=")

        def обход(m, условно):
            if not isinstance(m, dict) or "kind" not in m:
                return m
            раз = self.развернуть(m)
            k, оп = раз.get("kind"), раз.get("opcode", "")
            if присваивание(m):
                if условно:
                    небезопасно_замыкание[0] = True
                    return m
                дети = раз.get("inner", [])
                # RHS вычисляется ДО присваивания — обрабатываем первым, чтобы
                # вложенные присваивания встали в правильном порядке.
                if len(дети) >= 2:
                    раз["inner"][1] = обход(дети[1], False)
                присваивания.append(раз)
                return дети[0] if дети else m       # заменить на цель (LHS)
            if k == "BinaryOperator" and оп in ("&&", "||"):
                дети = раз.get("inner", [])
                if len(дети) >= 2:
                    раз["inner"][0] = обход(дети[0], условно)
                    раз["inner"][1] = обход(дети[1], True)   # правый операнд — условно
                return m
            if k == "ConditionalOperator":
                дети = раз.get("inner", [])
                if len(дети) >= 3:
                    раз["inner"][0] = обход(дети[0], условно)
                    раз["inner"][1] = обход(дети[1], True)
                    раз["inner"][2] = обход(дети[2], True)
                return m
            for i, c in enumerate(раз.get("inner", [])):
                if isinstance(c, dict) and "kind" in c:
                    раз["inner"][i] = обход(c, условно)
            return m

        условие2 = обход(copy.deepcopy(узел), False)
        return присваивания, условие2, not небезопасно_замыкание[0]

    def цикл_пока(self, n, ур):
        вн = n.get("inner", [])
        присв, усл2, безопасно = self._вынести_присваивания_условия(вн[0])
        if присв and not безопасно:
            self.добавить_пометку("присваивание-в-условии", n,
                                  деталь="за «&&»/«||»/«?:» в условии while", ур=ур)
            присв = []
        if присв:
            # «while ((x=f()) != NULL)» → «пока (истина) { x=f(); если(усл){…}иначе{прервать} }».
            # Konda НЕ имеет унарного «!», поэтому не отрицаем условие, а
            # оборачиваем тело в then-ветку, «прервать» — в «иначе» (заодно
            # «continue» в теле корректно возвращается на пере-вычисление условия).
            self.эмит(ур, "пока (истина) {")
            for a in присв:
                self.оператор_выражение(a, ур + 1)
            t = self._условие(усл2, ур + 1)
            self.эмит(ур + 1, f"если ({t}) {{")
            self.тело(вн[1], ур + 1)
            self.эмит(ур + 1, "} иначе { прервать }")
            self.эмит(ур, "}")
            return
        if self.небезопасен(вн[0]) or self.заблокирован(вн[0]):
            self.добавить_пометку("небезопасно-указатель", n,
                                 деталь="условие while", ур=ур)
            self.эмит(ур, "пока (истина) {")
            t = self._условие(вн[0], ур + 1)
            # без унарного «!»: тело — в then, «прервать» — в «иначе».
            self.эмит(ур + 1, f"если ({t}) {{")
            self.тело(вн[1], ур + 1)
            self.эмит(ур + 1, "} иначе { прервать }")
            self.эмит(ур, "}")
            return
        self.эмит(ур, f"пока ({self._истинность(вн[0])}) {{")
        self.тело(вн[1], ур)
        self.эмит(ур, "}")

    def цикл_делай(self, n, ур):
        вн = n.get("inner", [])
        тело, усл = вн[0], вн[1]
        присв, усл2, безопасно = self._вынести_присваивания_условия(усл)
        if присв and not безопасно:
            self.добавить_пометку("присваивание-в-условии", n,
                                  деталь="за «&&»/«||»/«?:» в условии do-while", ур=ур)
            присв = []
        self.эмит(ур, "пока (истина) {")
        self.тело(тело, ур)
        # присваивания из условия — после тела, ДО проверки (do-while: условие
        # вычисляется в конце итерации). Без унарного «!»: пустой then + «иначе».
        for a in присв:
            self.оператор_выражение(a, ур + 1)
        t = self._условие(усл2 if присв else усл, ур + 1)
        self.эмит(ур + 1, f"если ({t}) {{ }} иначе {{ прервать }}")
        self.эмит(ур, "}")

    def цикл_для(self, n, ур):
        вн = list(n.get("inner", []))
        while len(вн) < 5:
            вн.append({})
        init, _cv, cond, inc, body = вн[:5]
        # Присваивание в условии for: «for (…; (x=f()); …)». Переписываем в
        # «пока (истина)» (init до цикла, inc — в конце тела). НО если в теле есть
        # «continue» — в «пока» он пропустил бы шаг inc (семантика бы поехала),
        # тогда не переписываем и помечаем.
        присв, cond2, безопасно = (self._вынести_присваивания_условия(cond)
                                   if cond and "kind" in cond else ([], cond, True))
        if присв and (not безопасно or self._тело_имеет_continue(body)):
            self.добавить_пометку("присваивание-в-условии", n,
                                  деталь="for с присваиванием в условии (перепишите вручную)", ур=ур)
            присв = []
        if присв:
            # «for (init; (x=f()); inc)» → «init; пока (истина){ x=f();
            #  если(усл){ body; inc } иначе { прервать } }». Тело+inc — в then
            #  (Konda без унарного «!»); for с «continue» сюда не попадает (выше
            #  бракуется — иначе «continue» пропустил бы inc).
            init_s = self._часть(init)
            if init_s:
                self.эмит(ур, init_s)
            self.эмит(ур, "пока (истина) {")
            for a in присв:
                self.оператор_выражение(a, ур + 1)
            t = self._условие(cond2, ур + 1)
            self.эмит(ур + 1, f"если ({t}) {{")
            self.тело(body, ур + 1)
            inc_s = self._часть(inc)
            if inc_s:
                self.эмит(ур + 2, inc_s)
            self.эмит(ур + 1, "} иначе { прервать }")
            self.эмит(ур, "}")
            return
        # Оператор-запятая в for: «for(int i=0,j=10; …; i++,j--)». В Konda
        # заголовок несёт ОДНУ init и ОДИН шаг. Разбиваем: доп. объявления —
        # ДО цикла; доп. шаги — в КОНЕЦ тела. «continue» в C выполняет шаг, а
        # перенос доп. шагов в конец тела его пропустит → пометка ПРОВЕРИТЬ.
        перв_инит, доп_инит = self._части_инициализатора_for(init)
        шаги = self._разбить_запятую(inc) if inc and "kind" in inc else []
        if доп_инит or len(шаги) > 1:
            for стр in доп_инит:
                self.эмит(ур, стр)
            cond_s = self._истинность(cond) if cond and "kind" in cond else ""
            шаг_s = self.выражение(шаги[0]) if шаги else ""
            self.эмит(ур, f"для {перв_инит}; {cond_s}; {шаг_s} {{")
            self.тело(body, ур)
            if len(шаги) > 1 and self._тело_имеет_continue(body):
                self.добавить_пометку("запятая-в-for", n, ур=ур)
            for доп_шаг in шаги[1:]:
                self.оператор_выражение(доп_шаг, ур + 1)
            self.эмит(ур, "}")
            return
        init_s = self._часть(init)
        cond_s = self._истинность(cond) if cond and "kind" in cond else ""
        inc_s = self._часть(inc)
        if (cond and self.небезопасен(cond)) or (init and self.небезопасен(init)) \
                or (inc and self.небезопасен(inc)):
            self.добавить_пометку("небезопасно-указатель", n, деталь="часть for", ур=ур)
        # §38: цикл «для» пишется БЕЗ обрамляющих скобок вокруг заголовка
        # (старая форма «для (…)» снята из транспилятора — ошибка разбора).
        self.эмит(ур, f"для {init_s}; {cond_s}; {inc_s} {{")
        self.тело(body, ур)
        self.эмит(ур, "}")

    def _разбить_запятую(self, узел):
        """Плоский список операндов дерева оператора-запятой (иначе — [узел])."""
        раз = self.развернуть(узел) if isinstance(узел, dict) and "kind" in узел else узел
        if isinstance(раз, dict) and раз.get("kind") == "BinaryOperator" \
                and раз.get("opcode") == ",":
            рез = []
            for c in раз.get("inner", []):
                рез.extend(self._разбить_запятую(c))
            return рез
        return [узел] if isinstance(узел, dict) and "kind" in узел else []

    def _части_инициализатора_for(self, init):
        """for-init → (строка_для_заголовка, [доп_строки_объявлений_до_цикла]).
        «int i=0, j=10» (DeclStmt с >1) или «i=0, j=10» (запятая) — разбиваем."""
        if not isinstance(init, dict) or "kind" not in init:
            return "", []
        if init["kind"] == "DeclStmt":
            строки = []
            for d in init.get("inner", []):
                if not (isinstance(d, dict) and d.get("kind") == "VarDecl"):
                    continue
                kt = конда_тип(qualtype(d))
                имя = d.get("name", "_")
                вн = [c for c in d.get("inner", []) if isinstance(c, dict) and "kind" in c]
                строки.append(f"{kt} {имя} = {self.выражение(вн[-1])}" if вн
                              else f"{kt} {имя}")
            return (строки[0] if строки else ""), строки[1:]
        части = self._разбить_запятую(init)
        строки = [self.выражение(c) for c in части]
        return (строки[0] if строки else ""), строки[1:]

    def _часть(self, x):
        """for-init/step одной строкой (без переноса)."""
        if not x or "kind" not in x:
            return ""
        if x["kind"] == "DeclStmt":
            d = x.get("inner", [{}])[0]
            kt = конда_тип(qualtype(d))
            имя = d.get("name", "_")
            вн = [c for c in d.get("inner", []) if isinstance(c, dict) and "kind" in c]
            if вн:
                return f"{kt} {имя} = {self.выражение(вн[-1])}"
            return f"{kt} {имя}"
        return self.выражение(x)

    def выбор_в_если(self, n, ур):
        """C switch → цепочка если/иначе если (выбор Konda — только для enum)."""
        вн = n.get("inner", [])
        цель = self.выражение(вн[0])
        тело = вн[-1]
        if not тело or тело.get("kind") != "CompoundStmt":
            self.добавить_пометку("проверка-транспилятора", n,
                                 деталь="необычный switch", ур=ур)
            return
        группы, тек = [], None      # тек = (значения[] | None, операторы[])
        падение = False
        for c in тело.get("inner", []):
            k = c.get("kind")
            if k in ("CaseStmt", "DefaultStmt"):
                # clang вкладывает подряд идущие метки как substatement
                # предыдущей: «case A: case B: default: stmt». Разворачиваем ВСЕ
                # метки группы, спускаясь и через CaseStmt, и через DefaultStmt —
                # иначе состекованная метка (например «default: case X:») утекала
                # бы в операторы группы и ложно детектилась как провал.
                значения, есть_default, под = [], False, c
                while isinstance(под, dict) and \
                        под.get("kind") in ("CaseStmt", "DefaultStmt"):
                    пвн = под.get("inner", [])
                    if под.get("kind") == "DefaultStmt":
                        есть_default = True
                        под = пвн[-1] if пвн else {}
                    else:
                        значения.append(self.выражение(пвн[0]))
                        под = пвн[-1] if len(пвн) > 1 else {}
                # default ловит все значения → это ветка «иначе»; явные case,
                # состекованные с default, избыточны (default их и так покрывает).
                ярлык = None if есть_default else значения
                if тек is not None and тек[1] and not _завершён(тек[1]):
                    падение = True
                тек = (ярлык, [])
                группы.append(тек)
                if под and "kind" in под:
                    тек[1].append(под)
            else:
                if тек is None:
                    continue
                тек[1].append(c)
        if падение:
            self.добавить_пометку("switch-провал", n, ур=ур)
        # Konda не имеет «иначе если» → вложенная цепочка «иначе { если … }».
        значимые = [(зн, опы) for зн, опы in группы if зн is not None]
        умолчание = next((опы for зн, опы in группы if зн is None), None)
        self._цепочка_если(значимые, умолчание, цель, 0, ур)

    def _цепочка_если(self, значимые, умолчание, цель, i, ур):
        if i >= len(значимые):
            if умолчание is not None:
                self._операторы_switch(умолчание, ур)
            return
        значения, опы = значимые[i]
        усл = " или ".join(f"{цель} == {v}" for v in значения)
        self.эмит(ур, f"если ({усл}) {{")
        self._операторы_switch(опы, ур + 1)
        есть_ещё = (i + 1 < len(значимые)) or (умолчание is not None)
        if есть_ещё:
            self.эмит(ур, "} иначе {")
            self._цепочка_если(значимые, умолчание, цель, i + 1, ур + 1)
            self.эмит(ур, "}")
        else:
            self.эмит(ур, "}")

    def _операторы_switch(self, опы, ур):
        for c in опы:
            if c.get("kind") == "BreakStmt":
                continue                        # break — разделитель ветки, отбрасываем
            self.оператор(c, ур)

    def тело(self, n, ур):
        if not n or "kind" not in n:
            return
        if n["kind"] == "CompoundStmt":
            for c in n.get("inner", []):
                self.оператор(c, ур + 1)
        else:
            self.оператор(n, ур + 1)

    # ── верхний уровень ─────────────────────────────────────────────────────────
    def регистрация_записи(self, d):
        """Запоминает поля структуры / имя union до эмиссии тел."""
        имя = d.get("name")
        if not имя:
            return
        поля = [c.get("name") for c in d.get("inner", [])
                if c.get("kind") == "FieldDecl" and c.get("name")]
        if d.get("tagUsed") == "union":
            self.союзы.add(имя)
        self.поля_структур[имя] = поля
        # Типы полей — для «memset(&s, 0, sizeof(s))» → полевое обнуление.
        типы = []
        for c in d.get("inner", []):
            if c.get("kind") != "FieldDecl" or not c.get("name"):
                continue
            qt = без_квалификаторов(qualtype(c))
            разм = None
            if "[" in qt:
                m = re.search(r"\[(\d+)\]", qt)
                разм = int(m.group(1)) if m else None
                qt = qt.split("[")[0].strip()
            типы.append((c["name"], конда_тип(qt), разм))
        self.типы_полей[имя] = типы

    def структура(self, d):
        имя = d.get("name", "Аноним")
        ключ = "союз" if d.get("tagUsed") == "union" else "структура"
        # Поле-указатель-на-функцию без typedef («void (*run)(...)») раньше
        # деградировало в «символ**». Синтезируем «типфункции» с детерминиро-
        # ванным именем «Структура_поле» (это имя ТИПА, не функции — запрет
        # манглинга имён функций не про него) и печатаем перед структурой.
        синтез = {}
        for c in d.get("inner", []):
            if c.get("kind") != "FieldDecl" or not c.get("name"):
                continue
            m = re.match(r"(.+?)\(\*\)\((.*)\)$", без_квалификаторов(qualtype(c)))
            if m:
                возврат = конда_тип(m.group(1).strip())
                пар = [p.strip() for p in m.group(2).split(",")
                       if p.strip() and p.strip() != "void"]
                тфимя = f"{имя}_{c['name']}"
                синтез[c["name"]] = тфимя
                self._строка(f"типфункции {возврат} {тфимя}("
                             + ", ".join(конда_тип(p) for p in пар) + ")")
        if синтез:
            self._строка("")
        self._строка(f"{ключ} {имя} {{")
        for c in d.get("inner", []):
            if c.get("kind") != "FieldDecl":
                continue
            if c.get("name") in синтез:
                self._строка(f"    {синтез[c['name']]} {c['name']}")
                continue
            qt = qualtype(c)
            массив = "[" in без_квалификаторов(qt)
            if массив:
                баз = без_квалификаторов(qt).split("[")[0].strip()
                m = re.search(r"\[(\d*)\]", без_квалификаторов(qt))
                разм = m.group(1) if m else ""
                self._строка(f"    {конда_тип(баз)} {c.get('name','_')}[{разм}]")
            else:
                self._строка(f"    {конда_тип(qt)} {c.get('name','_')}")
        self._строка("}")
        self._строка("")

    def перечисление(self, d):
        имя = d.get("name")
        if not имя:
            # Анонимный enum в C — просто именованные целочисленные константы.
            # В Konda перечисление обязано иметь имя, поэтому разворачиваем в
            # глобальные «конст целое32 ИМЯ = знач» (тот же смысл, статически
            # иммутабельны). Значения: явные — как есть, авто — счётчик (+1).
            счётчик = 0
            есть = False
            for c in d.get("inner", []):
                if c.get("kind") != "EnumConstantDecl":
                    continue
                есть = True
                вн = [x for x in c.get("inner", [])
                      if isinstance(x, dict) and "kind" in x]
                if вн:
                    знач = self.выражение(вн[-1])
                    self._строка(f"конст целое32 {c.get('name')} = {знач}")
                    try:                        # обновить счётчик для авто-констант ниже
                        счётчик = int(знач, 0) + 1
                    except (ValueError, TypeError):
                        счётчик += 1
                else:
                    self._строка(f"конст целое32 {c.get('name')} = {счётчик}")
                    счётчик += 1
            if есть:
                self._строка("")
            return
        self._строка(f"перечисление {имя} {{")
        for c in d.get("inner", []):
            if c.get("kind") != "EnumConstantDecl":
                continue
            вн = [x for x in c.get("inner", []) if isinstance(x, dict) and "kind" in x]
            if вн:
                self._строка(f"    {c.get('name')} = {self.выражение(вн[-1])}")
            else:
                self._строка(f"    {c.get('name')}")
        self._строка("}")
        self._строка("")

    def typedef(self, d):
        имя = d.get("name", "?")
        qt = qualtype(d)
        # typedef указателя на функцию → типфункции
        m = re.match(r"(.+?)\(\*\)\((.*)\)$", без_квалификаторов(qt))
        if m:
            возврат = конда_тип(m.group(1).strip())
            параметры = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
            птипы = ", ".join(конда_тип(p) for p in параметры)
            self._строка(f"типфункции {возврат} {имя}({птипы})")
            self._строка("")
            return
        # typedef struct/enum — имя уже доступно; для простого псевдонима — пометка
        основа = без_квалификаторов(qt)
        if основа.startswith(("struct ", "union ", "enum ")):
            return                              # struct X {…} typedef — структура уже вышла
        # Псевдоним примитива/массива (typedef float GLfloat): в Konda alias для
        # примитива нет. Использования пока НЕ разворачиваются (это потребовало бы
        # десугаринга типов по всему файлу — отдельная задача), поэтому честно
        # помечаем: тип-псевдоним останется неразрешённым.
        self.добавить_пометку("typedef-алиас", d,
                             деталь=f"«{имя}» = «{основа}» → «{конда_тип(qt)}»")
        self._строка("")

    _ЦЕЛЫЕ_KONDA = ("целое8", "целое16", "целое32", "целое64", "байт", "логический")

    # ── распознавание C-идиомы listener → типизированные слушатели ──────────────

    @staticmethod
    def _имя_структуры(qt):
        """«const struct wl_buffer_listener *» → «wl_buffer_listener». Иначе None."""
        t = без_квалификаторов(qt).replace("*", " ")
        m = re.search(r"\bstruct\s+([A-Za-z_]\w*)", t)
        return m.group(1) if m else None

    def _каст_из_колбэка(self, func):
        """Первый оператор тела колбэка — «struct T *cv = data»? → (cv, T_c, каст_id).
        data — имя первого параметра (void*). Иначе None."""
        парамы = [p for p in func.get("inner", []) if p.get("kind") == "ParmVarDecl"]
        if not парамы:
            return None
        первый = парамы[0].get("name")
        if без_квалификаторов(qualtype(парамы[0])).strip() != "void *":
            return None
        тело = next((c for c in func.get("inner", []) if c.get("kind") == "CompoundStmt"), None)
        if not тело:
            return None
        for ст in тело.get("inner", []):
            if ст.get("kind") != "DeclStmt":
                continue
            vd = next((c for c in ст.get("inner", []) if c.get("kind") == "VarDecl"), None)
            if not vd:
                continue
            qt = qualtype(vd)
            if без_квалификаторов(qt).count("*") != 1:
                return None
            # инициализатор = первый параметр (data)
            вн = [c for c in vd.get("inner", []) if isinstance(c, dict) and "kind" in c]
            если_data = вн and self.базовое_имя(вн[-1]) == первый
            if not если_data:
                return None
            return (vd.get("name"), без_квалификаторов(qt).replace("*", "").strip(),
                    vd.get("id"), первый)
        return None

    def _анализ_слушателей(self, декларации, все_типы):
        """Находит регистрации «X_add_listener(obj, &G, data)», где G — глобал-
        структура типа S (поля — указатели на функции), а колбэки кастуют
        void*→T. Заполняет сл_типы/сл_экземпляры/сл_колбэки/сл_рег. Не
        распознанное остаётся сырым (безопасный fallback)."""
        глоб = {d.get("name"): d for d in декларации
                if d.get("kind") == "VarDecl" and d.get("name")}
        функции = {d.get("name"): d for d in декларации
                   if d.get("kind") == "FunctionDecl" and d.get("name")}
        # Структуры, которые kfc ПЕРЕэмитит как Konda-typedef (из .c или локальных
        # заголовков). Тип слушателя S должен оставаться C-«struct S» (его печатает
        # трампулин литералом), поэтому распознаём ТОЛЬКО слушателей из системных
        # заголовков (те kfc не переэмитит — «struct S» придёт через #содержит).
        # Иначе typedef-переопределение конфликтовало бы со «struct S».
        переэмит = {d.get("name") for d in декларации
                    if d.get("kind") == "RecordDecl" and d.get("name")}

        def распознать(call):
            дети = call.get("inner", [])
            if len(дети) != 4:            # callee + 3 аргумента
                return
            _f, а_obj, а_lst, а_data = дети
            adr = self.развернуть(а_lst)
            if adr.get("kind") != "UnaryOperator" or adr.get("opcode") != "&":
                return
            g = self.развернуть((adr.get("inner") or [{}])[0])
            if g.get("kind") != "DeclRefExpr":
                return
            G = self.имя_ссылки(g)
            gd = глоб.get(G)
            if not gd:
                return
            S = self._имя_структуры(qualtype(gd))
            if not S or S not in все_типы or S in переэмит:
                return       # переэмитируемую (локальную/typedef) структуру не трогаем:
                             # трампулин печатает «struct S» литералом, а typedef
                             # анонимной структуры конфликтовал бы с ним. Слушателей
                             # из системных (#содержит) заголовков — распознаём.
            # имена полей структуры S (в порядке объявления)
            имена_полей = [p.get("name") for p in все_типы[S].get("inner", [])
                           if p.get("kind") == "FieldDecl" and p.get("name")]
            # инициализатор G — список функций (позиционно/designated)
            вн = [c for c in gd.get("inner", []) if isinstance(c, dict) and "kind" in c]
            init = self.развернуть(вн[-1]) if вн else {}
            if init.get("kind") != "InitListExpr":
                return
            привязки = []      # (имя_поля, имя_функции)
            for i, эл in enumerate(init.get("inner", [])):
                фн = self.базовое_имя(эл)
                if not фн or фн not in функции:
                    return       # не имя функции проекта — не распознаём
                поле = имена_полей[i] if i < len(имена_полей) else None
                if not поле:
                    return
                привязки.append((поле, фн))
            if not привязки:
                return
            # T из касто-переменной колбэков (все должны совпасть)
            T_c = None
            колбэки_данные = {}
            for _поле, фн in привязки:
                инфо = self._каст_из_колбэка(функции[фн])
                if инфо is None:
                    return       # колбэк не по идиоме — не распознаём
                cv, tc, кид, void_p = инфо
                if T_c is None:
                    T_c = tc
                elif T_c != tc:
                    return       # разные T в одном слушателе — не распознаём
                колбэки_данные[фн] = (cv, tc, кид, void_p)
            if not T_c:
                return
            T_konda = конда_тип(T_c)
            листтип = S + "_сл"
            self.сл_типы[S] = {"листтип": листтип, "T_konda": T_konda,
                               "поля": [(поле, функции[фн]) for поле, фн in привязки]}
            self.сл_экземпляры[G] = {"S": S, "поля": привязки}
            for фн, (cv, tc, кид, _v) in колбэки_данные.items():
                self.сл_колбэки[фн] = {"T_konda": конда_тип(tc), "каст_var": cv,
                                       "каст_id": кид}
            self.сл_рег[call.get("id")] = (self.базовое_имя(_f), а_obj, G, а_data)

        def скан(n):
            if not isinstance(n, dict):
                return
            if n.get("kind") == "CallExpr":
                распознать(n)
            for c in n.get("inner", []):
                скан(c)
        for d in декларации:
            if d.get("kind") == "FunctionDecl":
                скан(d)

    def эмит_слушатель_типы(self):
        """Печатает «внешний слушатель<T> S_сл для S { поле(изменяемый T, …) … }»
        для каждого распознанного типа слушателя (наверху, до функций)."""
        for S, инфо in self.сл_типы.items():
            self._строка(f"внешний слушатель<{инфо['T_konda']}> {инфо['листтип']} "
                         f"для {S} {{")
            for имя_поля, колбэк in инфо["поля"]:
                парамы = [p for p in колбэк.get("inner", [])
                          if p.get("kind") == "ParmVarDecl"]
                части = [f"изменяемый {инфо['T_konda']} данные"]
                for p in парамы[1:]:            # [0] — void*data, заменён на T
                    kt = конда_тип(qualtype(p))
                    имя_п = p.get("name") or f"а{len(части)}"
                    части.append(f"{kt} {имя_п}")
                self._строка(f"    {имя_поля}(" + ", ".join(части) + ")")
            self._строка("}")
            self._строка("")

    def глобальная(self, d, ур=0):
        # Экземпляр слушателя: «S_сл G = { поле = функция, … }» вместо C-структуры.
        экз = self.сл_экземпляры.get(d.get("name"))
        if экз is not None:
            листтип = self.сл_типы[экз["S"]]["листтип"]
            привязки = ", ".join(f"{поле} = {фн}" for поле, фн in экз["поля"])
            self.эмит(ур, f"{листтип} {d.get('name')} = {{ {привязки} }}")
            return
        # Глобал верхнего уровня. В Konda обычных изменяемых глобалов НЕТ
        # (инвариант: гонки данных невыразимы). Два безопасных случая:
        #  * доказанно ИММУТАБЕЛЬНЫЙ → «конст» (static const в C);
        #  * ИЗМЕНЯЕМЫЙ целочисленный примитив → «атом<T>» (static _Atomic в C):
        #    атомарный доступ, гонок данных нет даже с потоками.
        # Прочие изменяемые (указатель/структура/массив) остаются без префикса —
        # понятная ошибка транспилятора (нужен другой механизм).
        имя = d.get("name")
        if имя in self.неизменяемые_глобали:
            self.объявление(d, ур, "конст ")
            return
        kt = конда_тип(qualtype(d))
        массив = "[" in без_квалификаторов(qualtype(d))
        if kt in self._ЦЕЛЫЕ_KONDA and not массив:
            # Изменяемый целочисленный глобал → атом<T>. Инициализатор — как есть
            # (обычно константа); объявление сформируем сами (объявление() не
            # знает про «атом<…>» как тип).
            вн = [c for c in d.get("inner", []) if isinstance(c, dict) and "kind" in c]
            иниц = вн[-1] if вн else None
            зн = self.выражение(иниц) if иниц is not None else "0"
            self.эмит(ур, f"атом<{kt}> {имя} = {зн}")
            return
        self.объявление(d, ур)

    @staticmethod
    def _тип_глобал_конст(qt):
        """Иммутабелен ли объект по типу (const ВЕРХНЕГО уровня): «const T»,
        «const T[N]» (массив const-элементов в C неизменяем), «T * const».
        False для «const T *» — сам указатель изменяем (const у pointee)."""
        if not re.search(r"\bconst\b", qt):
            return False
        голый = без_квалификаторов(qt)
        if "[" in голый or "*" not in голый:
            return True                     # массив / скаляр / структура
        return bool(re.search(r"\*\s*const\b", qt))

    def _глобал_конст_форма(self, qt):
        """Форма не-const глобала, которую «конст» транспилятора поддерживает
        (чтобы фактически-неизменяемый глобал перевести в конст): скаляр-примитив,
        структура (по значению), «символ*», массив примитивов/«символ*». Иначе
        False (массив структур, не-«символ*» указатель, многомерный) — такой не
        конвертируем, чтобы не породить битый вывод."""
        голый = без_квалификаторов(qt)
        if голый.count("[") > 1:
            return False                       # многомерный — конст не умеет
        массив = "[" in голый
        баз = голый.split("[")[0].strip() if массив else голый
        kt = конда_тип(баз)
        прим = (kt in self._ЦЕЛЫЕ_KONDA
                or kt in ("вещественное", "вещественное64", "символ"))
        if массив:
            return прим or kt == "символ*"
        if "*" in kt:
            return kt == "символ*"             # указатель-конст — только строка
        return True                            # примитив или структура-значение

    def _собрать_неизменяемые_глобали(self, декларации):
        """Имена глобалов, безопасных для «конст». Два источника:
        (1) C-`const`-квалифицированные (иммутабельны по типу верхнего уровня,
            либо указатель-на-const, ни разу не переприсваиваемый);
        (2) ФАКТИЧЕСКИ неизменяемые НЕ-const глобалы — ни разу не присваиваются И
            их адрес нигде не берётся (⇒ через указатель тоже не мутируются),
            причём форма поддержана «конст». Сканируем присваивания/inc/dec/«&»."""
        глоб = {d["name"]: qualtype(d) for d in декларации
                if d.get("kind") == "VarDecl" and d.get("name")}
        if not глоб:
            return set()
        присвоенные, адрес_взят = set(), set()
        def скан(n):
            if not isinstance(n, dict):
                return
            k, оп = n.get("kind"), n.get("opcode")
            if k in ("BinaryOperator", "CompoundAssignOperator") and оп \
                    and оп.endswith("=") and оп not in ("==", "!=", "<=", ">="):
                ц = self.базовое_имя(n.get("inner", [{}])[0])
                if ц:
                    присвоенные.add(ц)
            elif k == "UnaryOperator" and оп in ("++", "--"):
                ц = self.базовое_имя(n.get("inner", [{}])[0])
                if ц:
                    присвоенные.add(ц)
            elif k == "UnaryOperator" and оп == "&":
                ц = self.базовое_имя(n.get("inner", [{}])[0])
                if ц:
                    адрес_взят.add(ц)   # &g / &g.поле / &g[i] — возможная мутация
            for c in n.get("inner", []):
                скан(c)
        for d in декларации:
            if d.get("kind") == "FunctionDecl":
                скан(d)
        рез = set()
        for имя, qt in глоб.items():
            конст_qual = bool(re.search(r"\bconst\b", qt))
            if имя in присвоенные:
                continue                        # мутируется напрямую — не конст
            if конст_qual:
                if self._тип_глобал_конст(qt) or имя not in адрес_взят:
                    рез.add(имя)                # C-const: иммутабелен (или адрес не взят)
            elif имя not in адрес_взят and self._глобал_конст_форма(qt):
                рез.add(имя)                    # не-const, но доказанно неизменяем
        return рез

    def _указатель_декл_небезоп(self, n) -> bool:
        """Есть ли объявление указателя с небезопасным инициализатором —
        такое нельзя statement-обернуть (переменная уйдёт из области), значит
        всю функцию оборачиваем в «небезопасно { }»."""
        if not isinstance(n, dict) or "kind" not in n:
            return False
        if n["kind"] == "VarDecl":
            # Каст-переменная колбэка слушателя («struct T *cv = data») подавлена
            # (cv стал параметром «изменяемый T») — не считаем её сырой.
            if n.get("id") in getattr(self, "сл_подавить", ()):
                return False
            qt = без_квалификаторов(qualtype(n))
            вн = [c for c in n.get("inner", []) if isinstance(c, dict) and "kind" in c]
            иниц = вн[-1] if вн else None
            # исключаем malloc (→ срез), одиночную аллокацию (→ Ящик) и адрес-оф
            if "*" in qt and иниц is not None and self.небезопасен(иниц) \
                    and self._malloc_в_срез(n.get("name", ""), qualtype(n), иниц) is None \
                    and self._alloc_в_ящик(n.get("name", ""), qualtype(n), иниц) is None \
                    and not self.заблокирован(иниц):
                return True
        if n["kind"] in ("FunctionDecl",):
            return False
        return any(self._указатель_декл_небезоп(c)
                   for c in n.get("inner", []) if isinstance(c, dict))

    def _имя_индексируется(self, имя, n) -> bool:
        if not isinstance(n, dict) or "kind" not in n:
            return False
        if n["kind"] == "ArraySubscriptExpr":
            if self.базовое_имя(n.get("inner", [{}])[0]) == имя:
                return True
        return any(self._имя_индексируется(имя, c) for c in n.get("inner", []))

    def _имя_в_арифметике(self, имя, n) -> bool:
        """Указатель участвует в арифметике/переприсвоении/адрес-оф — не срез."""
        if not isinstance(n, dict) or "kind" not in n:
            return False
        k, оп = n["kind"], n.get("opcode")
        if k == "BinaryOperator" and оп in ("+", "-") and "*" in без_квалификаторов(qualtype(n)):
            for c in n.get("inner", []):
                if self.базовое_имя(c) == имя:
                    return True
        if k == "BinaryOperator" and оп == "=" and \
                self.базовое_имя(n.get("inner", [{}])[0]) == имя:
            return True
        if k == "UnaryOperator" and оп in ("++", "--", "*") and \
                self.базовое_имя(n.get("inner", [{}])[0]) == имя:
            return True
        return any(self._имя_в_арифметике(имя, c) for c in n.get("inner", []))

    def _обнаружить_goto_очистку(self, f, тело):
        """Паттерн «goto cleanup» (16 из 17 функций weston с goto): ОДНА метка в
        хвосте функции, ВСЕ goto ведут в неё, перед меткой — терминатор (return,
        нет проваливания в метку), хвост завершается return, и хвост ссылается
        только на ПАРАМЕТРЫ (не на локали функции). Такой хвост можно вынести в
        отдельную функцию «goto_<имя>», а каждый goto → «вернуть goto_<имя>(…)».
        → dict описания выноса, иначе None.

        Konda не имеет goto; это структурное переписывание в ранний выход
        (autofree сам освобождает срезы на выходе, поэтому cleanup-хвост обычно
        сводится к вызовам libc/проектных функций + return-кода)."""
        внутр = [c for c in тело.get("inner", []) if isinstance(c, dict)]
        метки = [(i, c) for i, c in enumerate(внутр) if c.get("kind") == "LabelStmt"]
        if len(метки) != 1:
            return None
        li, метка = метки[0]
        declid = метка.get("declId")
        if not declid or li == 0:
            return None
        # все goto функции ведут именно в эту метку
        gotos = []
        def собрать_goto(n):
            if isinstance(n, dict):
                if n.get("kind") == "GotoStmt":
                    gotos.append(n)
                for c in n.get("inner", []):
                    собрать_goto(c)
        собрать_goto(f)
        if not gotos or any(g.get("targetLabelDeclId") != declid for g in gotos):
            return None
        # перед меткой — явный return (в метку не проваливаются на нормальном пути)
        if внутр[li - 1].get("kind") != "ReturnStmt":
            return None
        # хвост = первый оператор метки + всё после неё
        хвост = [x for x in (метка.get("inner") or []) if isinstance(x, dict)]
        хвост += [x for x in внутр[li + 1:] if isinstance(x, dict)]
        if not хвост:
            return None
        возвр_ничего = (self.тип_возврата == "ничего")
        if not возвр_ничего and хвост[-1].get("kind") != "ReturnStmt":
            return None
        # Локали, объявленные до метки (могут понадобиться cleanup-у).
        локаль_декл = {}      # имя → (VarDecl, индекс_оператора)
        for i, c in enumerate(внутр[:li]):
            if c.get("kind") == "DeclStmt":
                for d in c.get("inner", []):
                    if isinstance(d, dict) and d.get("kind") == "VarDecl" and d.get("name"):
                        локаль_декл[d["name"]] = (d, i)
        имена = set()
        def собрать_имена(n):
            if isinstance(n, dict):
                if n.get("kind") == "DeclRefExpr":
                    rn = (n.get("referencedDecl") or {}).get("name")
                    if rn:
                        имена.add(rn)
                for c in n.get("inner", []):
                    собрать_имена(c)
        for x in хвост:
            собрать_имена(x)
        # Индекс первого top-level оператора, содержащего goto (для проверки
        # definite-assignment передаваемых локалей: они обязаны быть определены
        # ДО любого goto, иначе передача — использование до инициализации).
        def _есть_goto(n):
            if isinstance(n, dict):
                if n.get("kind") == "GotoStmt":
                    return True
                return any(_есть_goto(c) for c in n.get("inner", []))
            return False
        первый_goto = next((i for i, c in enumerate(внутр) if _есть_goto(c)),
                           len(внутр))
        # Параметры, на которые ссылается хвост, — параметры хелпера с ТЕМ ЖЕ
        # объявлением (возможно/изменяемый/вывод сохраняем 1:1 из основной
        # функции). Локали cleanup-а — по значению (объявлены с инициализатором
        # до первого goto, не срез). Срез (параметр или локаль) в cleanup не
        # поддерживаем (владение/освобождение сложнее) — bail.
        парамы = {p.get("name"): p for p in f.get("inner", [])
                  if p.get("kind") == "ParmVarDecl" and p.get("name")}
        # ДЕТЕРМИНИРОВАННЫЙ порядок параметров хелпера (иначе вывод kfc не
        # воспроизводим — «имена» это set): сперва ПАРАМЕТРЫ функции в порядке
        # объявления, затем ЛОКАЛИ cleanup-а в порядке их позиции в теле. Оба
        # берём только если хвост на них ссылается (имя ∈ «имена»).
        порядок = ([n for n in парамы if n in имена]
                   + sorted((n for n in локаль_декл if n in имена and n not in парамы),
                            key=lambda n: локаль_декл[n][1]))
        исп = []
        for имя_р in порядок:
            if имя_р in парамы:
                if имя_р in self.срез_переменные:
                    return None
                декл = self.декл_параметра.get(имя_р)
                if not декл:
                    return None
                исп.append((имя_р, декл))
            elif имя_р in локаль_декл:
                vd, поз = локаль_декл[имя_р]
                if поз >= первый_goto:
                    return None      # локаль объявлена после goto — не определена
                иниц = [x for x in vd.get("inner", [])
                        if isinstance(x, dict) and "kind" in x]
                if not иниц:
                    return None      # без инициализатора — use-before-init на goto
                if self._malloc_в_срез(имя_р, qualtype(vd), иниц[-1]) is not None:
                    return None      # локаль-срез — владение сложнее
                исп.append((имя_р, f"{конда_тип(qualtype(vd))} {имя_р}"))
            # иначе (глобаль/функция/строка-литерал) — передавать не нужно
        return {
            "declid": declid,
            "имя_метки": метка.get("name", "cleanup"),
            "helper": f"goto_{self.тек_исходное_имя}",
            "хвост": хвост,
            "li_id": метка.get("id"),
            "параметры": исп,     # [(имя, полное_объявление)]
            "возвр_ничего": возвр_ничего,
        }

    def _эмит_goto_хелпер(self, gc):
        """Эмитит функцию-хвост «goto_<имя>(параметры)» с телом cleanup-метки.
        Вызывается ПОСЛЕ основной функции (транспилятор разрешает прямые ссылки
        через signature-pass). Своё окружение: параметры сырые (по значению),
        без ссылок/срезов/подстановок."""
        сохр = (self.ссылочные_имена, self.срез_переменные, self.подстановки,
                self.переименования, self.goto_cleanup, self.внутри_небезопасно,
                self.освобождённые_срезы)
        self.ссылочные_имена = set(); self.срез_переменные = set()
        self.подстановки = {}; self.переименования = {}
        self.goto_cleanup = None; self.внутри_небезопасно = False
        self.освобождённые_срезы = set()
        # ref-параметры (изменяемый/вывод) в хелпере — их тела разыменовывают
        # безопасно; отметим, чтобы эмиссия не заворачивала в «небезопасно».
        for имя, декл in gc["параметры"]:
            if декл.startswith(("изменяемый ", "вывод ", "чтение ")):
                self.ссылочные_имена.add(имя)
        парам_стр = ", ".join(декл for _, декл in gc["параметры"])
        # Тип возврата — как у основной функции (в т.ч. возможно<T*>, иначе
        # «вернуть нуль» в хвосте не пройдёт по nullable-типу).
        if gc["возвр_ничего"]:
            тип = "ничего"
        elif self.возврат_возможно:
            тип = f"возможно<{self.тип_возврата}>"
        else:
            тип = self.тип_возврата
        self._строка(f"{тип} {gc['helper']}(" + парам_стр + ")")
        self._строка("{")
        for x in gc["хвост"]:
            self.оператор(x, 1)
        if gc["возвр_ничего"] and (not gc["хвост"]
                                   or gc["хвост"][-1].get("kind") != "ReturnStmt"):
            self.эмит(1, "вернуть")
        self._строка("}")
        self._строка("")
        (self.ссылочные_имена, self.срез_переменные, self.подстановки,
         self.переименования, self.goto_cleanup, self.внутри_небезопасно,
         self.освобождённые_срезы) = сохр

    def функция(self, f):
        # Таблица владения ключуется ИСХОДНЫМ именем C — берём его до
        # переименования точки входа, иначе подстановки для main не найдутся.
        исходное_имя = f.get("name", "?")
        имя = "точка_входа" if исходное_имя == "main" else исходное_имя
        self.тек_исходное_имя = исходное_имя
        self.тип_возврата = конда_тип(qualtype(f).split("(")[0].strip())
        self.возврат_возможно = (исходное_имя in self.нулевые.возвраты
                                 and self.тип_возврата.endswith("*"))
        self.срез_переменные = set()
        self.ящик_переменные = set()
        self.освобождённые_срезы = set()  # имена срезов, у которых уже сняли free
        self.ссылочные_имена = set()
        self.подстановки = self.владение.подстановки_функции(исходное_имя)
        self.переименования = {}
        self.goto_cleanup = None
        self.тек_функция = имя
        тело = next((c for c in f.get("inner", []) if c.get("kind") == "CompoundStmt"), None)
        self.тек_тело = тело   # для escape-скана Ящика в объявление()
        # Колбэк типизированного слушателя: первый параметр «void*data» станет
        # «изменяемый T каст_var», а декларация каста «struct T *cv = data»
        # подавляется (cv — теперь сам параметр).
        self.тек_колбэк = self.сл_колбэки.get(исходное_имя)
        self.сл_подавить = set()
        if self.тек_колбэк:
            self.сл_подавить.add(self.тек_колбэк["каст_id"])
        # §36: у точки входа сигнатура ЕДИНА — «целое32 точка_входа(срез<символ*>
        # аргументы)»: argv (включая имя программы), длина(аргументы) == argc.
        # Старая форма «(целое32 количество_аргументов, символ** аргументы)» снята
        # из транспилятора. Тело: argv → «аргументы» (срез, argv[i] → аргументы[i]),
        # argc → «длина(аргументы)». C-имена параметров main (argc/argv или любые)
        # переименовываем в теле; сам список параметров фиксирован.
        if имя == "точка_входа":
            вход_парамы = [c for c in f.get("inner", [])
                           if c.get("kind") == "ParmVarDecl"]
            подмена_входа = ["длина(аргументы)", "аргументы"]
            for i, c in enumerate(вход_парамы[:2]):
                if c.get("name"):
                    self.переименования[c["name"]] = подмена_входа[i]
            параметры = ["срез<символ*> аргументы"]
        else:
            параметры = []
        self.декл_параметра = {}   # сырое_имя → полное объявление (для goto-хелпера)
        индекс_п = -1
        for c in f.get("inner", []):
            if c.get("kind") != "ParmVarDecl":
                continue
            if имя == "точка_входа":
                continue           # §36: параметры точки входа — фиксированный срез
            индекс_п += 1
            # Колбэк слушателя: первый параметр «void*data» → «изменяемый T cv».
            if self.тек_колбэк and индекс_п == 0:
                T = self.тек_колбэк["T_konda"]
                cv = self.тек_колбэк["каст_var"]
                параметры.append(f"изменяемый {T} {cv}")
                self.ссылочные_имена.add(cv)
                self.декл_параметра[cv] = параметры[-1]
                continue
            # Исходное C-имя — для поиска в AST (индексация/арифметика ищутся по
            # нему); переименованное — для эмиссии в заголовок.
            сырое_имя = c.get("name", "_")
            pимя = self.переименования.get(сырое_имя, сырое_имя)
            qt = без_квалификаторов(qualtype(c))
            kt = конда_тип(qt)
            # указательный параметр → «срез<T>» (несёт длину, индексация
            # безопасна), если он индексируется и не участвует в арифметике,
            # ЛИБО если цикл проверки увидел «…теряет длину: объявите как срез».
            # Эвристика владения: «T *p» + всюду «f(&x)» → «изменяемый/вывод T p».
            # Кодоген сам поставит «*» в теле и «&» на вызове — сырого «&» нет.
            режим = self.владение.режим_параметра(исходное_имя, индекс_п)
            от_цикла = f"{имя}:{индекс_п}" in self.политика.срез_параметры
            эвристика = (тело is not None
                         and self._имя_индексируется(сырое_имя, тело)
                         and not self._имя_в_арифметике(сырое_имя, тело))
            if режим and kt.endswith("*"):
                параметры.append(f"{режим} {kt[:-1]} {pимя}")
                self.ссылочные_имена.add(сырое_имя)  # базовое_имя даёт сырое имя
            elif kt.endswith("*") and (от_цикла or эвристика):
                элем = kt[:-1]
                параметры.append(f"срез<{элем}> {pимя}")
                self.срез_переменные.add(сырое_имя)
            elif kt.endswith("*") and self.нулевые.нулевой(исходное_имя, сырое_имя):
                # параметр сравнивают с NULL / присваивают NULL → nullable
                параметры.append(f"возможно<{kt}> {pимя}")
            else:
                параметры.append(f"{kt} {pимя}")
            self.декл_параметра[сырое_имя] = параметры[-1]
        тип_в_заголовке = (f"возможно<{self.тип_возврата}>"
                           if self.возврат_возможно else self.тип_возврата)
        self._строка(f"{тип_в_заголовке} {имя}(" + ", ".join(параметры) + ")")
        if тело is None:
            self.строки[-1] = "// прототип: " + self.строки[-1]
            return
        self._строка("{")
        # Полная обёртка тела — либо по статической детекции (сырой указатель с
        # небезопасным инициализатором ушёл бы из области), либо по решению
        # цикла проверки (политика), который увидел реальную ошибку.
        полная = (имя in self.политика.полные_обёртки
                  or self._указатель_декл_небезоп(тело))
        if полная:
            self.добавить_пометку("небезопасно-указатель", f,
                                 деталь="тело функции целиком в «небезопасно» "
                                        "(сырой указатель живёт за пределами оператора)",
                                 ур=0)
            self.эмит(1, "небезопасно {")
            self.внутри_небезопасно = True
            for c in тело.get("inner", []):
                self.оператор(c, 2)
            self.внутри_небезопасно = False
            self.эмит(1, "}")
            умолч = ("нуль" if self.возврат_возможно
                     else значение_по_умолчанию(self.тип_возврата))
            if умолч is not None:
                self.эмит(1, f"вернуть {умолч}")
            elif self.тип_возврата != "ничего":
                self.добавить_пометку("проверка-транспилятора", f,
                                     деталь="функция возвращает указатель из "
                                            "небезопасного тела — добавьте «вернуть»",
                                     ур=1)
        else:
            # goto-cleanup: выносим хвост-метку в функцию «goto_<имя>», а сами
            # goto → «вернуть goto_<имя>(…)». Метку и её хвост в основном теле
            # не печатаем (они уехали в хелпер).
            self.goto_cleanup = self._обнаружить_goto_очистку(f, тело)
            стоп_id = self.goto_cleanup["li_id"] if self.goto_cleanup else None
            for c in тело.get("inner", []):
                if стоп_id is not None and c.get("id") == стоп_id:
                    break        # метка и всё после неё — в хелпере
                self.оператор(c, 1)
        self._строка("}")
        self._строка("")
        if self.goto_cleanup:
            self._эмит_goto_хелпер(self.goto_cleanup)
            self.goto_cleanup = None


# ─── драйвер ─────────────────────────────────────────────────────────────────
# Функции, которые НЕ возвращают управление (диверджентные) — вызов такой
# завершает поток не хуже break/return, поэтому провала (fallthrough) в
# следующий case нет. Без этого «case X: …; exit(1);» ложно помечался как провал.
_РАСХОДЯЩИЕСЯ = {
    "exit", "_exit", "_Exit", "quick_exit", "abort", "__builtin_unreachable",
    "__builtin_trap", "longjmp", "siglongjmp", "err", "errx", "verr", "verrx",
    "pthread_exit",
}


def _имя_вызова(n):
    """Имя вызываемой функции у CallExpr (сквозь ImplicitCast/Paren), иначе ''."""
    if not isinstance(n, dict):
        return ""
    вн = n.get("inner", [])
    if not вн:
        return ""
    ф = вн[0]
    while isinstance(ф, dict) and ф.get("kind") in (
            "ImplicitCastExpr", "ParenExpr", "CStyleCastExpr"):
        вд = ф.get("inner", [])
        if not вд:
            return ""
        ф = вд[0]
    if isinstance(ф, dict) and ф.get("kind") == "DeclRefExpr":
        return (ф.get("referencedDecl") or {}).get("name", "")
    return ""


def _расходится(c):
    """Оператор — вызов диверджентной функции (exit/abort/…)?"""
    if not isinstance(c, dict):
        return False
    if c.get("kind") == "CallExpr":
        return _имя_вызова(c) in _РАСХОДЯЩИЕСЯ
    # выражение-оператор оборачивает CallExpr напрямую
    return False


def _завершён(операторы):
    """Заканчивается ли список операторов терминатором потока: break/return/
    continue или вызов диверджентной функции (exit/abort/…)."""
    for c in reversed(операторы):
        if c.get("kind") in ("BreakStmt", "ReturnStmt", "ContinueStmt"):
            return True
        if _расходится(c):
            return True
        if c.get("kind") in ("CaseStmt", "DefaultStmt"):
            continue
        return False
    return False


def дамп_clang(путь, доп, игнорировать_ошибки=False):
    if not shutil.which("clang"):
        sys.stderr.write("ошибка: clang не найден в PATH\n")
        sys.exit(2)
    cmd = ["clang", "-Xclang", "-ast-dump=json", "-fsyntax-only", путь] + доп
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    # clang продолжает дамп и после fatal error (например, не найден заголовок),
    # но AST при этом НЕПОЛНЫЙ и типы в нём битые — молча переводить такое
    # значит выдать неверный код. По умолчанию отказываемся и показываем, чего
    # не хватило (обычно лечится «-- -Iкаталог -Dфлаг»).
    фаталы = [с for с in (proc.stderr or "").splitlines()
              if "fatal error" in с or " error:" in с]
    if фаталы and not игнорировать_ошибки:
        sys.stderr.write(
            f"ошибка: clang не смог полностью разобрать {путь} — перевод из "
            "неполного AST был бы молча неверен:\n")
        for с in фаталы[:10]:
            sys.stderr.write("  " + с + "\n")
        if len(фаталы) > 10:
            sys.stderr.write(f"  … и ещё {len(фаталы) - 10}\n")
        sys.stderr.write("подсказка: недостающие заголовки/макросы передаются "
                         "после «--» (например «-- -Iвключения -DФЛАГ»); "
                         "«--игнорировать-clang» продолжит вопреки ошибкам\n")
        sys.exit(2)
    if not proc.stdout:
        sys.stderr.write("ошибка clang:\n" + proc.stderr + "\n")
        sys.exit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"не удалось разобрать JSON clang: {e}\n")
        sys.exit(2)


def главные_объявления(корень, база):
    тек = None
    for c in корень.get("inner", []):
        f = c.get("loc", {}).get("file")
        if f:
            тек = f
        if тек and os.path.basename(тек) == база and not c.get("isImplicit"):
            yield c


def типы_из_локальных_заголовков(корень, база):
    """RecordDecl/EnumDecl/TypedefDecl из НЕсистемных заголовков (заголовки
    самого проекта: «дин_массив.h» и т.п.). В C структуры проекта живут в .h;
    без них перевод .c не компилируется. Системные (/usr/…) не трогаем — их
    типы придут через «#содержит»."""
    тек = None
    for c in корень.get("inner", []):
        f = c.get("loc", {}).get("file")
        if f:
            тек = f
        if not тек or os.path.basename(тек) == база or c.get("isImplicit"):
            continue
        if тек.startswith("/usr") or тек.startswith("<"):
            continue
        if c.get("kind") in ("RecordDecl", "EnumDecl", "TypedefDecl"):
            yield c




def нормализовать_позиции(узлы):
    """clang печатает номер строки только когда он сменился, и кладёт его то в
    «loc.line», то в «range.begin.line» (у операторов — обычно во второе, а
    вложенный VarDecl несёт лишь колонку). Разворачиваем наследование, проставляя
    каждому узлу loc.line, — нужно для поля C-ИСХОДНИК в пометках."""
    состояние = {"line": None}

    def обход(n):
        if not isinstance(n, dict):
            return
        нач = (n.get("range") or {}).get("begin") or {}
        if isinstance(нач.get("line"), int):
            состояние["line"] = нач["line"]
        loc = n.get("loc")
        if isinstance(loc, dict):
            if isinstance(loc.get("line"), int):
                состояние["line"] = loc["line"]
            elif состояние["line"] is not None:
                loc["line"] = состояние["line"]
        elif состояние["line"] is not None:
            n["loc"] = {"line": состояние["line"]}
        for c in n.get("inner", []):
            обход(c)
    for д in узлы:
        обход(д)


def индекс_узлов(декларации):
    """id узла → узел, чтобы вернуться от диагностики к месту в AST."""
    idx = {}

    def обход(n):
        if isinstance(n, dict):
            if n.get("id"):
                idx[n["id"]] = n
            for c in n.get("inner", []):
                обход(c)
    for d in декларации:
        обход(d)
    return idx


def _вызовы_внутри(n, найдено=None):
    найдено = найдено if найдено is not None else []
    if isinstance(n, dict):
        if n.get("kind") == "CallExpr":
            найдено.append(n)
        for c in n.get("inner", []):
            _вызовы_внутри(c, найдено)
    return найдено


def сгенерировать(декларации, политика, исходник_c,
                  таблица_влад=None, таблица_нул=None,
                  внешние_прототипы=None, внешние_типы=None, заголовки=None,
                  переэмит_заголовки=None, все_типы=None):
    """Один прогон эмиссии при заданной политике. → (текст, Конвертер).
    Таблицы анализов можно передать готовыми (многофайловый режим считает их
    по ВСЕМ декларациям проекта сразу); иначе считаются по этому файлу."""
    if таблица_влад is None:
        таблица_влад = влад.проанализировать(декларации, qualtype,
                                             без_квалификаторов, политика)
    if таблица_нул is None:
        таблица_нул = нул.проанализировать(декларации, qualtype,
                                           без_квалификаторов, политика)
    к = Конвертер(политика, исходник_c, таблица_влад, таблица_нул)
    # Иммутабельные глобалы (const-квалифицированные, не переприсваиваемые) →
    # печатаются с «конст» (иначе транспилятор отверг бы глобал-переменную).
    к.неизменяемые_глобали = к._собрать_неизменяемые_глобали(декларации)
    # Типизированные слушатели: распознаём C-идиому listener и переписываем в
    # «внешний слушатель<T>» + экземпляр + «слушать» (каст void*→T уходит в
    # сгенерированный трампулин транспилятора).
    к._анализ_слушателей(декларации, все_типы or {})
    for d in декларации:                       # 1-й проход: поля структур/union
        if d.get("kind") == "RecordDecl" and d.get("name"):
            к.регистрация_записи(d)
    # C-декларации «внешняя»-функций приходят из #include исходника (см. шапку
    # ниже) — сам транспилятор их прототип не печатает. Синтезировать
    # #содержит из заголовка-места не нужно (и он терял бы путь «GL/…»).
    _ = заголовки
    # Порядок объявлений (транспилятор парсит сверху вниз, forward-ссылок на
    # типы нет): внешний тип → типы (typedef/структуры/перечисления) → внешние
    # прототипы (их параметры-колбэки ссылаются на типфункции-typedef, а
    # изменяемый-параметры — на структуры) → функции.
    for имя_т in (внешние_типы or []):
        к._строка(f"внешний тип {имя_т}")
    if внешние_типы:
        к._строка("")
    # 2а: типы
    for d in декларации:
        k = d.get("kind")
        if k == "RecordDecl" and d.get("completeDefinition"):
            к.структура(d)
        elif k == "EnumDecl":
            к.перечисление(d)
        elif k == "TypedefDecl":
            к.typedef(d)
    # 2б: внешние прототипы (после типфункции/структур, до функций)
    for _имя, текст in (внешние_прототипы or []):
        к._строка(текст)
    if внешние_прототипы:
        к._строка("")
    # 2б.5: типы типизированных слушателей (ссылаются на T-структуру и C-тип).
    к.эмит_слушатель_типы()
    # 2в: функции и глобальные объявления
    заголовок = False
    for d in декларации:
        k = d.get("kind")
        if k == "FunctionDecl":
            if any(a.get("kind") == "CompoundStmt" for a in d.get("inner", [])):
                заголовок = True
            к.функция(d)
        elif k == "VarDecl":
            к.глобальная(d)
    if заголовок:                              # шапка — в те же списки, иначе
        # Переносим #include исходника: это реальные заголовки библиотек, из них
        # C-компилятор берёт декларации (в т.ч. для «внешняя»-функций, чей
        # прототип транспилятор не печатает). Путь сохраняется полностью
        # (с подкаталогом, не только базовое имя). stdio/stdlib — страховка.
        переэмит = переэмит_заголовки or set()
        инклюды = []
        for стр in к.исходник_c:
            m = re.match(r'\s*#\s*include\s*(<|")(.+?)(>|")', стр)
            if not m:
                continue
            системный, путь_вкл = m.group(1) == "<", m.group(2)
            # Локальный заголовок, чьи ТИПЫ kfc переэмитит (мф_счёт.h → структура
            # Счёт), не включаем — иначе C-typedef столкнётся с нашим. Заголовки
            # библиотек (внешняя-функции, системные) переносим: из них C берёт
            # декларации.
            if not системный and os.path.basename(путь_вкл) in переэмит:
                continue
            инклюды.append(f"#содержит {m.group(1)}{путь_вкл}{m.group(3)}")
        шапка = list(инклюды)
        for об in ("<stdio.h>", "<stdlib.h>"):
            if not any(об in и for и in инклюды):
                шапка.append(f"#содержит {об}")
        шапка.append("")                        # съедут номера
        к.строки[0:0] = шапка
        к.строка_узел[0:0] = [None] * len(шапка)
        к.строка_функц[0:0] = [None] * len(шапка)
        for п in к.пометки:                    # номера в отчёте — после сдвига
            п.строка += len(шапка)
    текст = "\n".join(к.строки).rstrip() + "\n"
    return текст, к


def _эскалация_срез_параметра(узел, политика):
    """«передача массива в сырой указательный параметр теряет длину» → найти
    вызов на этой строке и пометить параметр-приёмник массива как «срез<T>»."""
    изменено = False
    for вызов in _вызовы_внутри(узел):
        дети = вызов.get("inner", [])
        if not дети:
            continue
        # имя вызываемого — через referencedDecl первого ребёнка
        цель = дети[0]
        while isinstance(цель, dict) and цель.get("kind") in (
                "ImplicitCastExpr", "ParenExpr") and цель.get("inner"):
            цель = цель["inner"][-1]
        имя_ф = (цель.get("referencedDecl") or {}).get("name") if isinstance(цель, dict) else None
        if not имя_ф:
            continue
        for i, а in enumerate(дети[1:]):
            # аргумент-массив виден по распаду «ArrayToPointerDecay»
            if isinstance(а, dict) and а.get("kind") == "ImplicitCastExpr" \
                    and а.get("castKind") == "ArrayToPointerDecay":
                ключ = f"{имя_ф}:{i}"
                if ключ not in политика.срез_параметры:
                    политика.срез_параметры.add(ключ)
                    изменено = True
    return изменено


_ИМЯ_В_КАВЫЧКАХ = re.compile(r"«([^»]+)»")


def _ключ_параметра_по_имени(функции, имя_ф, имя_п):
    """(функция, имя параметра) → ключ «функция:индекс»."""
    ф = функции.get(имя_ф)
    if not ф:
        return None
    i = -1
    for c in ф.get("inner", []):
        if c.get("kind") != "ParmVarDecl":
            continue
        i += 1
        if c.get("name") == имя_п:
            return f"{имя_ф}:{i}"
    return None


def _ключ_параметра_по_аргументу(узел, имя_арг):
    """Диагностика на ВЫЗОВЕ называет переменную вызывающего («изменяемый»-
    аргумент «x»…). Находим вызов и позицию этого аргумента → «функция:индекс»."""
    for вызов in _вызовы_внутри(узел):
        дети = вызов.get("inner", [])
        if not дети:
            continue
        цель = дети[0]
        while isinstance(цель, dict) and цель.get("kind") in (
                "ImplicitCastExpr", "ParenExpr") and цель.get("inner"):
            цель = цель["inner"][-1]
        имя_ф = (цель.get("referencedDecl") or {}).get("name") \
            if isinstance(цель, dict) else None
        if not имя_ф:
            continue
        for i, а in enumerate(дети[1:]):
            if влад._база_имя(а) == имя_арг:
                return f"{имя_ф}:{i}"
    return None


def _применить_ссылку(действие, д, к, узел, политика, функции):
    """Правила про «изменяемый»/«вывод». → было ли изменение политики."""
    что = действие.split(":", 1)[1]
    имена = _ИМЯ_В_КАВЫЧКАХ.findall(д.текст)
    ключ = None
    if что == "изменяемый":
        # «выходной параметр «p» не присвоен…» — имя ПАРАМЕТРА, функция из карты строк
        имя_ф = к.функция_на_строке(д.строка)
        for им in имена:
            ключ = ключ or _ключ_параметра_по_имени(функции, имя_ф, им)
    else:
        # диагностика на вызове называет переменную ВЫЗЫВАЮЩЕГО
        for им in имена:
            if узел is not None:
                ключ = ключ or _ключ_параметра_по_аргументу(узел, им)
        if ключ is None:
            имя_ф = к.функция_на_строке(д.строка)
            for им in имена:
                ключ = ключ or _ключ_параметра_по_имени(функции, имя_ф, им)
    if not ключ:
        return False
    if что == "отмена":
        if ключ not in политика.отмена_ссылки:
            политика.отмена_ссылки.add(ключ)
            return True
        return False
    if политика.режим_ссылки.get(ключ) == что:
        # уже пробовали этот режим — значит ссылка не подходит вовсе
        if ключ not in политика.отмена_ссылки:
            политика.отмена_ссылки.add(ключ)
            return True
        return False
    политика.режим_ссылки[ключ] = что
    return True


def _имя_указателя_из_узла(узел):
    """Имя указателя, о котором говорит диагностика без имени в тексте:
    объявление → имя переменной; присваивание → имя цели."""
    if not isinstance(узел, dict):
        return None
    if узел.get("kind") == "VarDecl" and узел.get("name"):
        return узел["name"]
    if узел.get("kind") == "BinaryOperator" and узел.get("opcode") == "=":
        return влад._база_имя((узел.get("inner") or [{}])[0])
    for c in узел.get("inner", []):
        имя = _имя_указателя_из_узла(c)
        if имя:
            return имя
    return None


def _применить_возможно(действие, д, к, узел, политика):
    """Правила про «возможно<T*>». → было ли изменение политики."""
    имя_ф = к.функция_на_строке(д.строка)
    if not имя_ф:
        return False
    if имя_ф == "точка_входа":
        имя_ф = "main"                 # анализы ключуются C-именем
    что = действие.split(":", 1)[1]
    if что == "включить":
        # «указатель ненулевой по умолчанию …» — имени в тексте нет,
        # берём его из узла (объявление или цель присваивания)
        имя = _имя_указателя_из_узла(узел)
        if имя and (имя_ф, имя) not in политика.включить_возможно:
            политика.включить_возможно.add((имя_ф, имя))
            return True
        return False
    # «разыменование указателя «p», который может быть нулевым» — снять
    # СЛАБОЕ свидетельство (одно лишь сравнение); сильное отменять нельзя,
    # без «возможно» код не скомпилируется вовсе (нулевые.py это учитывает)
    изменено = False
    имена = [и for и in _ИМЯ_В_КАВЫЧКАХ.findall(д.текст)
             if и not in ("возможно", "нуль", "если")]
    if not имена:
        имена = [и for и in (_имя_указателя_из_узла(узел),) if и]
    for имя in имена:
        if (имя_ф, имя) not in политика.отмена_возможно:
            политика.отмена_возможно.add((имя_ф, имя))
            изменено = True
    return изменено


def применить_диагностики(диаги, к, политика, индекс, функции):
    """Диагностики транспилятора → изменения политики. → было ли изменение."""
    изменено = False
    for д in диаги:
        действие = пров.действие_по_ошибке(д.текст)
        узел_id = к.узел_на_строке(д.строка)
        узел = индекс.get(узел_id) if узел_id else None
        if действие and действие.startswith("ссылка:"):
            if _применить_ссылку(действие, д, к, узел, политика, функции):
                изменено = True
        elif действие and действие.startswith("возможно:"):
            if _применить_возможно(действие, д, к, узел, политика):
                изменено = True
        elif действие == "небезопасно" and узел_id:
            if узел_id not in политика.небезоп_узлы:
                политика.небезоп_узлы.add(узел_id)
                изменено = True
        elif действие == "срез_параметр" and узел is not None:
            if _эскалация_срез_параметра(узел, политика):
                изменено = True
        elif действие == "полная_обёртка":
            имя_ф = к.функция_на_строке(д.строка)
            # Если в функции уже есть непереводимое место (адрес-оф/goto), эта
            # ошибка — его следствие: обёртка не поможет, только зря снимет
            # безопасность. Оставляем пометку (ниже, в «сдаться»).
            if имя_ф and имя_ф not in к.функции_с_блокировкой \
                    and имя_ф not in политика.полные_обёртки:
                политика.полные_обёртки.add(имя_ф)
                изменено = True
        elif действие and действие.startswith("непереводимо:"):
            код = действие.split(":", 1)[1]
            if узел_id and узел_id not in политика.пометки_узлы:
                политика.пометки_узлы[узел_id] = (код, None, д.текст)
                изменено = True
    return изменено


def сдаться(диаги, к, политика):
    """Нерешённые диагностики → пометки KONDA-TODO на соответствующих узлах."""
    for д in диаги:
        узел_id = к.узел_на_строке(д.строка)
        if узел_id and узел_id not in политика.пометки_узлы:
            политика.пометки_узлы[узел_id] = ("проверка-транспилятора", None, д.текст)


def _именовать_анонимные(декларации):
    """Идиома C «typedef struct {…} Имя;»: тег анонимен, имя даёт typedef.
    Переносим имя typedef на саму struct/enum — Konda объявит «структура Имя».
    Вызывать ДО перепривязки id: ссылка typedef→decl идёт по исходному id."""
    безымянные = {}
    for д in декларации:
        if д.get("kind") in ("RecordDecl", "EnumDecl") and not д.get("name") \
                and д.get("id"):
            безымянные[д["id"]] = д

    def найти_ссылку(n):
        if not isinstance(n, dict):
            return None
        дк = n.get("decl")
        if isinstance(дк, dict) and дк.get("id") in безымянные:
            return дк["id"]
        for c in n.get("inner", []):
            р = найти_ссылку(c)
            if р:
                return р
        return None

    for д in декларации:
        if д.get("kind") != "TypedefDecl" or not д.get("name"):
            continue
        ид = найти_ссылку(д)
        if ид and not безымянные[ид].get("name"):
            безымянные[ид]["name"] = д["name"]


def _перепривязать_id(узлы, префикс):
    """Уникализирует id узлов clang между файлами проекта: id — это адреса
    памяти clang и МОГУТ совпасть между независимыми запусками, а политика
    и карта строк ключуются по id."""
    def обход(n):
        if isinstance(n, dict):
            if "id" in n:
                n["id"] = префикс + str(n["id"])
            for c in n.get("inner", []):
                обход(c)
    for д in узлы:
        обход(д)


def собрать_локальные_enum(декларации):
    """EnumDecl, объявленные ВНУТРИ функций (локальный тип), — в Konda enum
    живёт только на верхнем уровне. Возвращает такие узлы (именованные, с
    полным определением) для выноса наверх; дубли по имени отсеиваются."""
    out, имена = [], set()
    def обход(n, внутри_функции):
        if not isinstance(n, dict):
            return
        k = n.get("kind")
        # У ЛОКАЛЬНОГО enum clang не ставит completeDefinition — определением
        # считаем наличие хотя бы одной EnumConstantDecl.
        есть_конст = any(isinstance(c, dict) and c.get("kind") == "EnumConstantDecl"
                         for c in n.get("inner", []))
        if k == "EnumDecl" and внутри_функции and n.get("name") \
                and есть_конст and n.get("name") not in имена:
            имена.add(n["name"])
            # Глубокая копия: тот же узел остаётся в теле функции (объявление()
            # его пропустит), а наверх идёт независимая копия — иначе
            # _перепривязать_id дважды префиксует общий id.
            out.append(copy.deepcopy(n))
        for c in n.get("inner", []):
            обход(c, внутри_функции or k == "FunctionDecl")
    for д in декларации:
        обход(д, False)
    return out


class Единица:
    """Один .c-файл проекта: AST, исходник, карта узлов и результат эмиссии."""

    def __init__(self, путь, доп, номер, имя, игнорировать_clang=False):
        self.путь = путь
        self.имя = имя                       # базовое имя выходного .конда
        корень = дамп_clang(путь, доп, игнорировать_clang)
        база = os.path.basename(путь)
        # типы из заголовков проекта — ПЕРЕД объявлениями самого файла
        главные = list(главные_объявления(корень, база))
        # Локальные (объявленные в теле функций) enum — тип верхнего уровня в
        # Konda: выносим наверх (в теле объявление() их пропускает).
        локальные_enum = собрать_локальные_enum(главные)
        self.декларации = (list(типы_из_локальных_заголовков(корень, база))
                           + локальные_enum + главные)
        # Базовые имена заголовков, ЧЬИ СТРУКТУРЫ/перечисления kfc переэмитит
        # (мф_счёт.h → структура Счёт): их #include НЕ переносим — анонимный
        # C-typedef структуры («typedef struct {…} Счёт») при повторном
        # объявлении конфликтует с нашим. Указатель-на-функцию typedef (Обработчик)
        # НЕ конфликтует (идентичное переопределение допустимо), а заголовок с
        # ним может нести нужные «внешняя»-функции — такой включаем. Файл
        # отслеживаем наследованием loc.file (clang печатает его лишь при смене).
        self.переэмит_заголовки = set()
        тек = None
        for c in корень.get("inner", []):
            f = (c.get("loc") or {}).get("file")
            if f:
                тек = f
            if not тек or os.path.basename(тек) == база or c.get("isImplicit"):
                continue
            if тек.startswith("/usr") or тек.startswith("<"):
                continue
            if c.get("kind") in ("RecordDecl", "EnumDecl"):
                self.переэмит_заголовки.add(os.path.basename(тек))
        # Прототипы и типы из ВСЕХ заголовков (и системных): источник сигнатур
        # для «внешняя»-генерации и определений для замыкания типов. Анонимные
        # «typedef struct {…} Имя» именуются по всему корню ДО сбора.
        _именовать_анонимные(корень.get("inner", []))
        self.все_прототипы = {}   # имя → (FunctionDecl без тела, файл-заголовок)
        self.все_типы = {}        # имя → RecordDecl(complete)/EnumDecl
        тек_файл = None
        for c in корень.get("inner", []):
            f = (c.get("loc") or {}).get("file")
            if f:
                тек_файл = f
            k = c.get("kind")
            имя_у = c.get("name")
            if not имя_у:
                continue
            # Функции ЗАГОЛОВКОВ — кандидаты во «внешняя» (и прототипы, и
            # static inline с телом); функции самого .c отсеются позже — они
            # «определённые» в проекте и переводятся обычным путём.
            if k == "FunctionDecl" and имя_у not in self.все_прототипы:
                self.все_прототипы[имя_у] = (c, тек_файл)
            elif k in ("RecordDecl", "EnumDecl") and c.get("completeDefinition") \
                    and имя_у not in self.все_типы:
                self.все_типы[имя_у] = c
        _именовать_анонимные(self.декларации)
        _перепривязать_id(self.декларации, f"ф{номер}_")
        try:
            with open(путь, encoding="utf-8", errors="replace") as fh:
                self.исходник = fh.read().splitlines()
        except OSError:
            self.исходник = []
        нормализовать_позиции(self.декларации)
        self.индекс = индекс_узлов(self.декларации)
        self.текст = ""
        self.к = None


_ПРИМИТИВЫ_КОНДА = {"целое8", "целое16", "целое32", "целое64", "байт",
                    "вещественное", "вещественное64", "логический", "символ",
                    "ничего"}


def _имя_польз_типа(qt):
    """Имя пользовательского типа в qualType (без «*»/массива), иначе None."""
    т = без_квалификаторов(qt).replace("*", " ").split("[")[0]
    т = (т.replace("struct ", "").replace("union ", "")
          .replace("enum ", "").strip())
    if not т or "(" in т or " " in т:
        return None
    kt = конда_тип(т).replace("неподписанный ", "")
    return None if kt in _ПРИМИТИВЫ_КОНДА else т


def _разобрать_фнптр(qt):
    """qualType указателя на функцию «RET (*)(ARGS)» → (возврат_kt, [пар_kt]),
    иначе None. Для колбэк-параметров внешних функций (регистраторов слушателей)."""
    m = re.match(r"(.+?)\(\*\)\((.*)\)$", без_квалификаторов(qt))
    if not m:
        return None
    возврат = конда_тип(m.group(1).strip())
    параметры = [p.strip() for p in m.group(2).split(",")
                 if p.strip() and p.strip() != "void"]
    return возврат, [конда_тип(p) for p in параметры]


def _арг_имя_функции(а):
    """Аргумент — голое имя функции (колбэк): «g(display)» → «display», иначе None."""
    a = влад._развернуть(а)
    if isinstance(a, dict) and a.get("kind") == "DeclRefExpr":
        rd = a.get("referencedDecl") or {}
        if rd.get("kind") == "FunctionDecl":
            return rd.get("name")
    return None


def собрать_внешние_прототипы(единицы, все_декл, определённые):
    """Функции, ВЫЗЫВАЕМЫЕ с «&x», но определённые вне проекта (библиотека) →
    Konda-прототипы «внешняя … (изменяемый/чтение …)»: кодоген транспилятора сам
    ставит «&» на вызове, сырой адрес-оф исчезает. Сигнатура берётся из
    clang-прототипа заголовка. → ({id(единицы): [(имя, строка_прототипа)]},
    множество id снятых «&»)."""
    вызовы = влад.собрать_вызовы(все_декл)
    по_единице = {id(е): [] for е in единицы}
    снятые = set()
    реф_типы = {id(е): set() for е in единицы}   # нужны ПОЛНЫЕ определения
    указ_имена = set()                            # кандидаты в «внешний тип»
    заголовки = {id(е): set() for е in единицы}
    # Имена typedef-ов указателя на функцию, которые kfc САМ эмитит как
    # «типфункции» (из локальных заголовков) — их можно использовать в колбэк-
    # параметре по имени, не синтезируя новый тип.
    тф_типдефы = set()
    for е in единицы:
        for д in е.декларации:
            if д.get("kind") == "TypedefDecl" and д.get("name") \
                    and _разобрать_фнптр(qualtype(д)):
                тф_типдефы.add(д["name"])
    # Байтовые libc-функции НЕ конвертируем: их void* — сырые байты с размером
    # третьим аргументом, типизация по одному вызову неверна в принципе; к тому
    # же у memset есть свой путь (полевое обнуление), который & должен видеть.
    БАЙТОВЫЕ = {"memset", "memcpy", "memmove", "memcmp", "memchr",
                "bcopy", "bzero", "qsort", "bsearch", "free", "realloc"}
    for имя, список in вызовы.items():
        if имя in определённые or имя in БАЙТОВЫЕ:
            continue
        пара = next(((е,) + е.все_прототипы[имя] for е in единицы
                     if имя in е.все_прототипы), None)
        if пара is None:
            continue
        е, узел, файл_заг = пара
        if узел.get("variadic"):
            continue
        парамы = [c for c in узел.get("inner", [])
                  if c.get("kind") == "ParmVarDecl"]
        режимы, колбэки, годен = {}, {}, True
        for i, п in enumerate(парамы):
            qt = без_квалификаторов(qualtype(п))
            арги = [а[i] for а, _в in список if len(а) > i]
            # Колбэк: параметр — указатель на функцию, и ВСЕ вызовы передают
            # голое имя функции ПРОЕКТА (регистрация обработчика). Если тип —
            # именованный typedef, который kfc сам эмитит как «типфункции»,
            # используем его по имени; иначе (анонимный «RET (*)(ARGS)»)
            # синтезируем «типфункции». Транспилятор принимает имя функции в
            # такой параметр — проверка сигнатуры статическая.
            qt_пар = qualtype(п)
            десугар = (п.get("type") or {}).get("desugaredQualType")
            именованный = qt_пар if qt_пар in тф_типдефы else None
            фнптр = (_разобрать_фнптр(qt_пар)
                     or (_разобрать_фнптр(десугар) if десугар else None))
            if (именованный or фнптр is not None) and арги:
                имена_ф = [_арг_имя_функции(а) for а in арги]
                if all(имена_ф) and all(н in определённые for н in имена_ф):
                    колбэки[i] = ("имя", именованный) if именованный \
                        else ("синтез", фнптр)
                    continue
            адреса = [влад.адрес_lvalue(а) for а in арги]
            if not арги or not any(адреса):
                continue
            # Сигнатура одна на все вызовы: позиция становится ссылкой, только
            # если «&lvalue» дают ВСЕ вызовы. Неконвертируемая позиция больше
            # НЕ отменяет функцию целиком — она остаётся сырым указателем, а
            # её «&»-аргументы остаются с пометкой (другие позиции выигрывают).
            звёзд = qt.count("*")
            if not all(адреса) or "[" in qt or звёзд not in (1, 2):
                continue
            реж_слово = "чтение" if "const " in qualtype(п) else "изменяемый"
            баз = qt.replace("*", " ").strip()
            if звёзд == 1 and баз not in ("void", "char"):
                # Обычный «T*» — тип из объявления параметра.
                режимы[i] = (реж_слово, None, None)
                continue
            # «void *data» (1 звезда, GUI-паттерн user-data) и «T**» (2 звезды,
            # out-указатель: weston_config_*(&str), get(&ptr)). Тип берём из
            # АРГУМЕНТОВ: «&x» указывает в x, тип x — и есть тип ссылки Konda.
            # C-декларация из заголовка не меняется (void*/T**): «&» снимет
            # кодоген транспилятора, конверсия T*→void* / T*→T** легальна.
            типы = set()
            for ад in адреса:
                вну = (ад.get("inner") or [{}])[0]
                t = вну.get("type") if isinstance(вну, dict) else None
                типы.add(без_квалификаторов(t.get("qualType", ""))
                         if isinstance(t, dict) else "")
            if len(типы) != 1:
                continue                        # разные типы аргументов — оставить сырым
            т_c = типы.pop()
            if not т_c or "[" in т_c:
                continue
            if звёзд == 1:
                # void*: только простой тип T (иначе — байты/строки/массив указ.)
                if "*" in т_c or т_c in ("void", "char"):
                    continue
            else:
                # T**: аргумент — «&(указатель)», тип ровно с одной «*»
                if т_c.count("*") != 1:
                    continue
            режимы[i] = (реж_слово, конда_тип(т_c), т_c)
        # Возврат — указатель на ПОЛЬЗОВАТЕЛЬСКИЙ/opaque тип (напр. opendir →
        # DIR*, readdir → dirent*)? Тогда прототип нужен, даже без «&»-параметров:
        # транспилятору без него неизвестен тип результата, и присваивание
        # «возможно<DIR*> dir = opendir(...)» не проходит проверку типов.
        возврат_qt = qualtype(узел).split("(")[0].strip()
        возврат_польз_указ = bool(_имя_польз_типа(возврат_qt)
                                  and "*" in возврат_qt)
        if not годен or (not режимы and not колбэки and not возврат_польз_указ):
            continue
        части = []
        for i, п in enumerate(парамы):
            имя_п = п.get("name") or f"п{i}"
            if i in колбэки:
                вид, данные = колбэки[i]
                if вид == "имя":
                    части.append(f"{данные} {имя_п}")  # kfc уже эмитит типфункции
                else:
                    возврат_кб, пар_кб = данные
                    тф_имя = f"{имя}_{имя_п}"   # детерминированное имя ТИПА
                    по_единице[id(е)].append(
                        (None, f"типфункции {возврат_кб} {тф_имя}("
                               + ", ".join(пар_кб) + ")"))  # определение — перед внешней
                    части.append(f"{тф_имя} {имя_п}")
            elif i in режимы:
                режим_и, кт_данных, т_c_данных = режимы[i]
                if кт_данных is not None:
                    # типизированная void*-позиция: тип — из аргументов вызовов
                    имя_т = _имя_польз_типа(т_c_данных)
                    if имя_т:
                        реф_типы[id(е)].add(имя_т)
                    части.append(f"{режим_и} {кт_данных} {имя_п}")
                    continue
                # ref-позиция: базовый тип обязан быть ПОЛНЫМ (замыкание)
                баз = конда_тип(без_квалификаторов(qualtype(п))
                                .replace("*", " ").strip())
                имя_т = _имя_польз_типа(qualtype(п))
                if имя_т:
                    реф_типы[id(е)].add(имя_т)
                части.append(f"{режим_и} {баз} {имя_п}")
            else:
                имя_т = _имя_польз_типа(qualtype(п))
                if имя_т:
                    указ_имена.add(имя_т)
                части.append(f"{конда_тип(qualtype(п))} {имя_п}")
        имя_вт = _имя_польз_типа(возврат_qt)   # возврат_qt посчитан выше (до skip)
        if имя_вт:
            указ_имена.add(имя_вт)
        возврат = конда_тип(возврат_qt)
        по_единице[id(е)].append(
            (имя, f"внешняя {возврат} {имя}(" + ", ".join(части) + ")"))
        if файл_заг:
            # Декларацию для C даёт заголовок — «#содержит» его в вывод
            # (kfc сам прототип в C не печатает: он бы конфликтовал со
            # static inline / макро-обёртками реального заголовка).
            заголовки[id(е)].add(файл_заг)
        for а, _в in список:
            for i in режимы:
                if len(а) > i:
                    ад = влад.адрес_lvalue(а[i])
                    if ад is not None and ад.get("id"):
                        снятые.add(ад["id"])
    return по_единице, снятые, реф_типы, указ_имена, заголовки


def замкнуть_типы(единицы, реф_типы=None):
    """Замыкание by-value: поле локальной структуры типа из СИСТЕМНОГО заголовка
    требует определения — добавляем его декларацию в единицу (рекурсивно).
    «реф_типы» — дополнительные затравки: базовые типы «изменяемый»/«чтение»-
    позиций внешних прототипов. Возвращает множество имён всех известных типов."""
    # ВАЖНО: RecordDecl без тела (forward-декларация «struct Имя;») — НЕ
    # известный тип. Его нельзя эмитить структурой, а если счесть известным, он
    # выпадет из «внешний тип» и оставит «возможно<Имя*>» с неопределённым
    # именем. Такой тип используется только через указатель → он непрозрачный.
    def _полный(д):
        if д.get("kind") == "RecordDecl":
            return bool(д.get("completeDefinition"))
        return д.get("kind") in ("EnumDecl", "TypedefDecl")
    известные = {д.get("name") for е in единицы for д in е.декларации
                 if _полный(д) and д.get("name")}
    for е in единицы:
        очередь = [д for д in е.декларации if д.get("kind") == "RecordDecl"]
        for имя_т in (реф_типы or {}).get(id(е), set()):
            if имя_т not in известные and имя_т in е.все_типы:
                новый = е.все_типы[имя_т]
                е.декларации.insert(0, новый)
                известные.add(имя_т)
                очередь.append(новый)
        while очередь:
            д = очередь.pop()
            for поле in д.get("inner", []):
                if поле.get("kind") != "FieldDecl":
                    continue
                qt = без_квалификаторов(qualtype(поле))
                if "*" in qt:
                    continue
                имя_т = _имя_польз_типа(qt)
                if имя_т and имя_т not in известные and имя_т in е.все_типы:
                    новый = е.все_типы[имя_т]
                    е.декларации.insert(0, новый)
                    известные.add(имя_т)
                    очередь.append(новый)
        е.индекс = индекс_узлов(е.декларации)
    return известные


def собрать_непрозрачные(единицы, известные, доп_имена):
    """Типы, упомянутые ТОЛЬКО через указатель и не определённые в проекте
    (непрозрачные C-хендлы) → «внешний тип Имя» (объявление без устройства;
    транспилятор статически запрещает by-value). → отсортированный список."""
    упомянутые = set(доп_имена)

    def учесть(qt):
        if "*" not in без_квалификаторов(qt):
            return
        имя_т = _имя_польз_типа(qt)
        if имя_т:
            упомянутые.add(имя_т)

    for е in единицы:
        for д in е.декларации:
            if д.get("kind") == "RecordDecl":
                for поле in д.get("inner", []):
                    if поле.get("kind") == "FieldDecl":
                        учесть(qualtype(поле))
            elif д.get("kind") == "FunctionDecl":
                учесть(qualtype(д).split("(")[0])
                for п in д.get("inner", []):
                    if п.get("kind") == "ParmVarDecl":
                        учесть(qualtype(п))

        def тела(n):
            if isinstance(n, dict):
                if n.get("kind") == "VarDecl":
                    учесть(qualtype(n))
                for c in n.get("inner", []):
                    тела(c)
        for д in е.декларации:
            if д.get("kind") == "FunctionDecl":
                тела(д)
    return sorted(упомянутые - известные)


def конвертировать_проект(пути, доп_по_файлам, транспилятор=None, проверять=True,
                          макс_итераций=6, игнорировать_clang=False):
    """C-проект (1..N файлов) → Konda. Анализы владения и nullable считаются по
    ОБЩЕМУ графу вызовов: сигнатура функции из файла А, вызываемой с «&x» из
    файла Б, переводится так же, как внутри одного файла.

    Проверка транспилятором — по КОНКАТЕНАЦИИ всех .конда (семантически то же,
    что слияние AST при «Транспилятор а.конда б.конда», но диагностики после
    слияния не несут имени файла — по смещениям конкатенации строка однозначно
    возвращается в свой файл). → (единицы, оставшиеся_диагностики, итераций)."""
    занято, единицы = set(), []
    for i, путь in enumerate(пути):
        имя = os.path.splitext(os.path.basename(путь))[0]
        while имя in занято:
            имя += "_2"
        занято.add(имя)
        единицы.append(Единица(путь, доп_по_файлам.get(путь, []), i, имя,
                               игнорировать_clang))

    # Общий заголовок виден из нескольких .c → его типы попали бы в несколько
    # .конда, а транспилятор сливает файлы в одну программу (тип должен быть
    # объявлен один раз). Оставляем первое вхождение каждого имени.
    если_видели = set()
    for е in единицы:
        оставить = []
        for д in е.декларации:
            if д.get("kind") in ("RecordDecl", "EnumDecl", "TypedefDecl") \
                    and д.get("name"):
                ключ = (д["kind"], д["name"])
                if ключ in если_видели:
                    continue
                если_видели.add(ключ)
            оставить.append(д)
        е.декларации = оставить
        е.индекс = индекс_узлов(е.декларации)

    все_декл = [д for е in единицы for д in е.декларации]
    # Одноимённые функции С ТЕЛОМ в разных файлах (static-дубли) — их сигнатуры
    # не трогаем: граф вызовов по имени их не различает.
    определений = {}
    функции = {}
    for д in все_декл:
        if д.get("kind") == "FunctionDecl" and д.get("name"):
            функции.setdefault(д["name"], д)
            if any(c.get("kind") == "CompoundStmt" for c in д.get("inner", [])):
                определений[д["name"]] = определений.get(д["name"], 0) + 1
    коллизии = {и for и, н in определений.items() if н > 1}

    # Внешние прототипы («&x» в библиотечные функции), замыкание by-value типов
    # и «внешний тип» для непрозрачных указателей — до эмиссии.
    определённые = set(определений)
    внеш_по_ед, снятые_внеш, реф_типы, указ_имена, заголовки_внеш = \
        собрать_внешние_прототипы(единицы, все_декл, определённые)
    известные_типы = замкнуть_типы(единицы, реф_типы)
    непрозрачные = собрать_непрозрачные(единицы, известные_типы, указ_имена)
    все_декл = [д for е in единицы for д in е.декларации]

    политика = Политика()

    def сгенерировать_все():
        таблица = влад.проанализировать(все_декл, qualtype, без_квалификаторов,
                                        политика, исключённые=коллизии)
        таблица.снятые_амперсанды |= снятые_внеш
        нулевые = нул.проанализировать(все_декл, qualtype, без_квалификаторов,
                                       политика)
        for номер, е in enumerate(единицы):
            е.текст, е.к = сгенерировать(
                е.декларации, политика, е.исходник, таблица, нулевые,
                внешние_прототипы=внеш_по_ед.get(id(е)),
                внешние_типы=(непрозрачные if номер == 0 else None),
                заголовки=sorted(заголовки_внеш.get(id(е), ())),
                переэмит_заголовки=е.переэмит_заголовки,
                все_типы=е.все_типы)

    def собрать_общий():
        """Конкатенация + карта «глобальная строка → (единица, локальная)»."""
        куски, границы, сдвиг = [], [], 0
        for е in единицы:
            куски.append(е.текст)
            строк = len(е.текст.splitlines())
            границы.append((сдвиг, сдвиг + строк, е))
            сдвиг += строк
        return "".join(куски), границы

    def локализовать(границы, глоб_строка):
        for нач, кон, е in границы:
            if нач < глоб_строка <= кон:
                return е, глоб_строка - нач
        return единицы[0], глоб_строка

    сгенерировать_все()
    if not проверять or not транспилятор:
        return единицы, [], 0

    for итерация in range(1, макс_итераций + 1):
        общий, границы = собрать_общий()
        библиотека = not any("точка_входа(" in е.текст for е in единицы)
        диаги = пров.прогнать(транспилятор, общий, библиотека=библиотека)
        if not диаги:
            return единицы, [], итерация
        до = политика.отпечаток()
        for д in диаги:
            е, д.строка = локализовать(границы, д.строка)
            применить_диагностики([д], е.к, политика, е.индекс, функции)
            д.единица = е
        if политика.отпечаток() == до:
            # новых исправлений нет — оставляем пометки на местах ошибок
            for д in диаги:
                сдаться([д], д.единица.к, политика)
            сгенерировать_все()
            return единицы, диаги, итерация
        сгенерировать_все()

    общий, границы = собрать_общий()
    библиотека = not any("точка_входа(" in е.текст for е in единицы)
    диаги = пров.прогнать(транспилятор, общий, библиотека=библиотека)
    if диаги:
        for д in диаги:
            е, д.строка = локализовать(границы, д.строка)
            сдаться([д], е.к, политика)
        сгенерировать_все()
    return единицы, диаги, макс_итераций


def конвертировать(путь, доп, транспилятор=None, проверять=True, макс_итераций=6):
    """Однофайловый вход (обратная совместимость).
    → (текст, Конвертер, оставшиеся_диагностики, число_итераций)."""
    единицы, диаги, итераций = конвертировать_проект(
        [путь], {путь: доп}, транспилятор, проверять, макс_итераций)
    е = единицы[0]
    return е.текст, е.к, диаги, итераций


def _из_compile_commands(путь, доп):
    """compile_commands.json → [(файл.c, флаги clang)]. Берём только флаги,
    влияющие на препроцессор/стандарт: -I/-D/-U/-std/-include; относительные
    -I разрешаются от «directory» записи."""
    with open(путь, encoding="utf-8") as fh:
        записи = json.load(fh)
    рез = []
    for з in записи:
        ф = з.get("file", "")
        if not ф.endswith(".c"):
            continue
        кат = з.get("directory", ".")
        файл = ф if os.path.isabs(ф) else os.path.normpath(os.path.join(кат, ф))
        арги = з.get("arguments") or (з.get("command") or "").split()
        флаги = []
        i = 0
        while i < len(арги):
            а = арги[i]
            if а in ("-I", "-D", "-U", "-include") and i + 1 < len(арги):
                зн = арги[i + 1]
                if а == "-I" and not os.path.isabs(зн):
                    зн = os.path.normpath(os.path.join(кат, зн))
                флаги += [а, зн]
                i += 2
                continue
            if а.startswith("-I") and len(а) > 2:
                тело = а[2:]
                if not os.path.isabs(тело):
                    тело = os.path.normpath(os.path.join(кат, тело))
                флаги.append("-I" + тело)
            elif а.startswith(("-D", "-U", "-std=")):
                флаги.append(а)
            i += 1
        рез.append((файл, флаги + доп))
    return рез


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 3
    пути, вывод, отчёт, доп = [], None, None, []
    проверять, макс_итераций, явный_тр = True, 6, None
    игнорировать_clang = False
    i = 1
    while i < len(argv):
        а = argv[i]
        if а == "-o" and i + 1 < len(argv):
            вывод = argv[i + 1]; i += 2
        elif а == "--отчёт" and i + 1 < len(argv):
            отчёт = argv[i + 1]; i += 2
        elif а == "--транспилятор" and i + 1 < len(argv):
            явный_тр = argv[i + 1]; i += 2
        elif а == "--итераций" and i + 1 < len(argv):
            макс_итераций = max(1, int(argv[i + 1])); i += 2
        elif а == "--без-проверки":
            проверять = False; i += 1
        elif а == "--игнорировать-clang":
            игнорировать_clang = True; i += 1
        elif а == "--":
            доп = argv[i + 1:]; break
        elif а.startswith("-"):
            i += 1
        else:
            пути.append(а); i += 1

    # compile_commands.json разворачивается в список его файлов
    доп_по_файлам, развернутые = {}, []
    for п in пути:
        if not os.path.exists(п):
            sys.stderr.write(f"файл не найден: {п}\n")
            return 3
        if os.path.basename(п) == "compile_commands.json":
            for ф, флаги in _из_compile_commands(п, доп):
                if not os.path.exists(ф):
                    sys.stderr.write(f"файл не найден (из compile_commands): {ф}\n")
                    return 3
                развернутые.append(ф)
                доп_по_файлам[ф] = флаги
        else:
            развернутые.append(п)
            доп_по_файлам[п] = доп
    пути = развернутые
    if not пути:
        sys.stderr.write("не указано ни одного файла .c\n")
        return 3
    if len(пути) > 1 and not вывод:
        sys.stderr.write("многофайловый режим: укажите каталог вывода через -o\n")
        return 3

    транспилятор = пров.найти_транспилятор(явный_тр) if проверять else None
    if проверять and not транспилятор:
        sys.stderr.write("предупреждение: транспилятор не найден — цикл проверки "
                         "выключен (укажите --транспилятор ПУТЬ или "
                         "KONDA_ТРАНСПИЛЯТОР)\n")
    единицы, диаги, итераций = конвертировать_проект(
        пути, доп_по_файлам, транспилятор,
        проверять and bool(транспилятор), макс_итераций, игнорировать_clang)

    # ── запись результата ────────────────────────────────────────────────────
    имена_вывода = {}
    if len(единицы) == 1 and not (вывод and os.path.isdir(вывод)):
        е = единицы[0]
        имена_вывода[е.имя] = вывод or "-"
        if вывод:
            with open(вывод, "w", encoding="utf-8") as fh:
                fh.write(е.текст)
            sys.stderr.write(f"записано: {вывод}\n")
        else:
            sys.stdout.write(е.текст)
    else:
        os.makedirs(вывод, exist_ok=True)
        for е in единицы:
            путь_к = os.path.join(вывод, е.имя + ".конда")
            имена_вывода[е.имя] = путь_к
            with open(путь_к, "w", encoding="utf-8") as fh:
                fh.write(е.текст)
        sys.stderr.write(f"записано: {len(единицы)} файлов в {вывод}/\n")

    if отчёт:
        части = [(е.к.пометки, е.путь, имена_вывода[е.имя]) for е in единицы]
        with open(отчёт, "w", encoding="utf-8") as fh:
            fh.write(пм.отчёт_json_несколько(части))

    # Сводка в stderr. Важная тонкость: непереводимый фрагмент уходит в
    # комментарий, поэтому остаток МОЖЕТ пройти транспилятор — но поведение
    # программы при этом молча изменится. Не называем это успехом.
    пометки_все = [п for е in единицы for п in е.к.пометки]
    блок = sum(1 for п in пометки_все if п.категория in пм.БЛОКИРУЮЩИЕ)
    if not транспилятор:
        итог = "без проверки"
    elif диаги:
        итог = "НЕ проходит транспилятор"
    elif блок:
        итог = ("проходит транспилятор, НО поведение изменено "
                "(непереводимое закомментировано)")
    else:
        итог = "принят транспилятором"
    sys.stderr.write(f"итог: {итог}")
    if итераций:
        sys.stderr.write(f" (итераций цикла: {итераций})")
    sys.stderr.write(f"; пометок: {len(пометки_все)}")
    if блок:
        sys.stderr.write(f" (из них блокируют компиляцию: {блок})")
    sys.stderr.write("\n")
    for кат in (пм.НЕПЕРЕВОДИМО, пм.ОШИБКА, пм.НЕБЕЗОПАСНО, пм.ПРОВЕРИТЬ):
        н = sum(1 for п in пометки_все if п.категория == кат)
        if н:
            sys.stderr.write(f"  {кат}: {н}\n")
    if отчёт:
        sys.stderr.write(f"отчёт для ИИ/трекера: {отчёт}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
