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
import json
import os
import re
import shutil
import subprocess
import sys

import владение as влад
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
        self.пометки_узлы = {}         # id узла → пм.Пометка (сдались)

    def отпечаток(self):
        return (frozenset(self.небезоп_узлы), frozenset(self.полные_обёртки),
                frozenset(self.срез_параметры), frozenset(self.пометки_узлы),
                frozenset(self.режим_ссылки.items()), frozenset(self.отмена_ссылки))

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


def qualtype(n) -> str:
    t = n.get("type")
    if isinstance(t, dict):
        return t.get("qualType", "int")
    if isinstance(t, str):
        return t
    return "int"


def без_квалификаторов(t: str) -> str:
    t = t.replace("const", " ").replace("volatile", " ").replace("restrict", " ")
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
    def __init__(self, политика=None, исходник_c=None, владение=None):
        self.строки = []
        self.строка_узел = []       # параллельно строкам: id узла-владельца строки
        self.строка_функц = []      # параллельно строкам: имя функции-владельца
        self.политика = политика or Политика()
        self.владение = владение or влад.ТаблицаВладения()
        self.ссылочные_имена = set()  # параметры-ссылки текущей функции (изменяемый/вывод)
        self.подстановки = {}       # локальный указатель → узел цели («T *p = &x»)
        self.исходник_c = исходник_c or []   # строки исходного .c (для C-ИСХОДНИК)
        self.пометки = []           # список пм.Пометка, попавших в вывод
        self.союзы = set()          # имена union-типов (для детекции доступа к члену)
        self.поля_структур = {}     # имя структуры → [имена полей] (для { поле = знач })
        self.срез_переменные = set()  # переменные, ставшие срезом (malloc) — индекс безопасен
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
           n.get("castKind") == "BitCast":
            return True                          # reinterpret указателя
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

    # ── выражения ───────────────────────────────────────────────────────────────
    def выражение(self, n) -> str:
        n = self.развернуть(n)
        if not n or "kind" not in n:
            return ""
        k = n["kind"]
        вн = n.get("inner", [])
        if k == "IntegerLiteral":
            return n.get("value", "0")
        if k == "FloatingLiteral":
            return n.get("value", "0.0")
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
            return имя_д
        if k == "MemberExpr":
            основа = self.выражение(вн[0]) if вн else ""
            # p->f и s.f в Konda оба через «.» (кодоген расставит доступ сам)
            return f"{основа}.{n.get('name', '?')}"
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
            return f"{self.выражение(вн[0])} {оп} {self.выражение(вн[1])}"
        if k == "CompoundAssignOperator":
            л, п = self.выражение(вн[0]), self.выражение(вн[1])
            оп = n.get("opcode", "+=")[:-1]
            return f"{л} = {л} {оп} {п}"
        if k == "UnaryOperator":
            опнд = self.выражение(вн[0])
            оп = n.get("opcode", "?")
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
            self.заметки += 1
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
        self.заметки += 1
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
            self.добавить_пометку("goto", n, ур=ур)
            return
        if k in ("LabelStmt",):
            self.добавить_пометку("goto", n, деталь=f"метка «{n.get('name','?')}»", ур=ур)
            for c in вн:
                self.оператор(c, ур)
            return
        # выражение-оператор (вызов/присваивание/инкремент)
        self.оператор_выражение(n, ур)

    def оператор_выражение(self, n, ур):
        # free(срез) отбрасываем — у среза автоосвобождение (autofree)
        внр = self.развернуть(n)
        if внр.get("kind") == "CallExpr":
            дети = внр.get("inner", [])
            if дети and self.базовое_имя(дети[0]) == "free" and len(дети) == 2:
                if self.базовое_имя(дети[1]) in self.срез_переменные:
                    return
        причина = self.причина_блокировки(n)
        if причина:
            self.добавить_пометку("адрес-оф", n, деталь="в выражении-операторе", ур=ур)
            self.эмит(ур, "// " + self.выражение(n))
            return
        строка = self.выражение(n)
        if self.небезопасен(n) and not self.внутри_небезопасно:
            self.эмит(ур, "небезопасно { " + строка + " }")
        else:
            self.эмит(ур, строка)

    def объявление(self, d, ур):
        if d.get("kind") == "RecordDecl":       # вложенная struct/union — верхнеуровнево
            return
        if d.get("id") in self.владение.пропустить_объявления:
            return          # «T *p = &x» — вместо «p» печатаем «x» (подстановка)
        if d.get("kind") != "VarDecl":
            self.добавить_пометку("проверка-транспилятора", d,
                                 деталь=f"объявление {d.get('kind')} пропущено", ур=ур)
            return
        имя = d.get("name", "_")
        qt = qualtype(d)
        kt = конда_тип(qt)
        вн = [c for c in d.get("inner", []) if isinstance(c, dict) and "kind" in c]
        иниц = вн[-1] if вн else None
        массив = "[" in без_квалификаторов(qt)
        разм = ""
        if массив:
            m = re.search(r"\[(\d*)\]", без_квалификаторов(qt))
            разм = m.group(1) if m else ""
            баз = без_квалификаторов(qt).split("[")[0].strip()
            kt = конда_тип(баз)

        # malloc/calloc → срез<T> имя = выделить(N)
        срез = self._malloc_в_срез(имя, qt, иниц)
        if срез is not None:
            self.срез_переменные.add(имя)
            self.эмит(ур, срез)
            return

        объ = f"{kt} {имя}"
        if массив:
            объ += f"[{разм}]"
        if иниц is None:
            self.эмит(ур, объ)
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
                self.заметки += 1
            else:
                self.эмит(ур, f"{объ} = {умолч}")
                self.эмит(ур, "небезопасно { " + f"{имя} = {self.выражение(иниц)}" + " }")
            return
        self.эмит(ур, f"{объ} = {self.выражение(иниц)}")

    def _malloc_в_срез(self, имя, qt, иниц):
        """T* p = (T*)malloc(N*sizeof(T)) → срез<T> p = выделить(N). Иначе None."""
        if иниц is None:
            return None
        узел = self.развернуть(иниц)
        if узел.get("kind") != "CallExpr":
            return None
        вн = узел.get("inner", [])
        if not вн:
            return None
        имя_ф = self.базовое_имя(вн[0]) or ""
        if имя_ф not in ("malloc", "calloc"):
            return None
        т = без_квалификаторов(qt)
        if "*" not in т:
            return None
        элем = конда_тип(т[:т.rindex("*")].strip())
        аргс = вн[1:]
        счёт = self._счёт_из_malloc(имя_ф, аргс)
        return f"срез<{элем}> {имя} = выделить({счёт})"

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
            self.заметки += 1
            return self.выражение(аргс[0]) + " /* TODO(konda): проверьте число элементов */"
        return "0"

    def _тернарник_в_если(self, объ, имя, терн, ур):
        вн = терн.get("inner", [])
        self.эмит(ур, объ)
        self.эмит(ур, f"если ({self.выражение(вн[0])}) {{")
        self.эмит(ур + 1, f"{имя} = {self.выражение(вн[1])}")
        self.эмит(ур, "} иначе {")
        self.эмит(ур + 1, f"{имя} = {self.выражение(вн[2])}")
        self.эмит(ур, "}")

    def возврат(self, выр, ур):
        if выр is None:
            self.эмит(ур, "вернуть")
            return
        причина = self.причина_блокировки(выр)
        if причина:
            self.добавить_пометку("адрес-оф", выр, деталь="в выражении «вернуть»", ур=ур)
            self.эмит(ур, f"// вернуть {self.выражение(выр)}")
            умолч = значение_по_умолчанию(self.тип_возврата)
            self.эмит(ур, f"вернуть {умолч if умолч else '0'}")
            return
        внр = self.развернуть(выр)
        if внр.get("kind") == "ConditionalOperator":
            вн = внр.get("inner", [])
            self.эмит(ур, f"если ({self.выражение(вн[0])}) {{")
            self.эмит(ур + 1, f"вернуть {self.выражение(вн[1])}")
            self.эмит(ур, "} иначе {")
            self.эмит(ур + 1, f"вернуть {self.выражение(вн[2])}")
            self.эмит(ур, "}")
            return
        if self.небезопасен(выр) and not self.внутри_небезопасно:
            умолч = значение_по_умолчанию(self.тип_возврата)
            if умолч is None:
                # возврат указателя из небезопасного выражения — вся функция обёрнута
                self.эмит(ур, f"вернуть {self.выражение(выр)}")
                self.заметки += 1
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
        return self.выражение(узел)

    def если(self, n, ур):
        вн = n.get("inner", [])
        усл = self._условие(вн[0], ур)
        self.эмит(ур, f"если ({усл}) {{")
        self.тело(вн[1], ур)
        if len(вн) > 2 and isinstance(вн[2], dict) and "kind" in вн[2]:
            if вн[2]["kind"] == "IfStmt":
                self.эмит(ур, "} иначе {")
                self.если(вн[2], ур + 1)
                self.эмит(ур, "}")
                return
            self.эмит(ур, "} иначе {")
            self.тело(вн[2], ур)
        self.эмит(ур, "}")

    def цикл_пока(self, n, ур):
        вн = n.get("inner", [])
        if self.небезопасен(вн[0]) or self.заблокирован(вн[0]):
            self.добавить_пометку("небезопасно-указатель", n,
                                 деталь="условие while", ур=ур)
            self.эмит(ур, "пока (истина) {")
            t = self._условие(вн[0], ур + 1)
            self.эмит(ур + 1, f"если (!{t}) {{ прервать }}")
            self.тело(вн[1], ур)
            self.эмит(ур, "}")
            return
        self.эмит(ур, f"пока ({self.выражение(вн[0])}) {{")
        self.тело(вн[1], ур)
        self.эмит(ур, "}")

    def цикл_делай(self, n, ур):
        вн = n.get("inner", [])
        тело, усл = вн[0], вн[1]
        self.эмит(ур, "пока (истина) {")
        self.тело(тело, ур)
        t = self._условие(усл, ур + 1)
        self.эмит(ур + 1, f"если (!({t})) {{ прервать }}")
        self.эмит(ур, "}")

    def цикл_для(self, n, ур):
        вн = list(n.get("inner", []))
        while len(вн) < 5:
            вн.append({})
        init, _cv, cond, inc, body = вн[:5]
        init_s = self._часть(init)
        cond_s = self.выражение(cond) if cond and "kind" in cond else ""
        inc_s = self._часть(inc)
        if (cond and self.небезопасен(cond)) or (init and self.небезопасен(init)) \
                or (inc and self.небезопасен(inc)):
            self.добавить_пометку("небезопасно-указатель", n, деталь="часть for", ур=ур)
        self.эмит(ур, f"для ({init_s}; {cond_s}; {inc_s}) {{")
        self.тело(body, ур)
        self.эмит(ур, "}")

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
        группы, тек = [], None      # тек = (значения[], операторы[])
        падение = False
        for c in тело.get("inner", []):
            k = c.get("kind")
            if k in ("CaseStmt", "DefaultStmt"):
                # стек «case A: case B:» — CaseStmt вложены
                значения, под = [], c
                while под.get("kind") == "CaseStmt":
                    пвн = под.get("inner", [])
                    значения.append(self.выражение(пвн[0]))
                    под = пвн[-1] if len(пвн) > 1 else {}
                if c.get("kind") == "DefaultStmt":
                    значения = None
                    под = c.get("inner", [{}])[-1] if c.get("inner") else {}
                if тек is not None and тек[1] and not _завершён(тек[1]):
                    падение = True
                тек = (значения, [])
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

    def структура(self, d):
        имя = d.get("name", "Аноним")
        ключ = "союз" if d.get("tagUsed") == "union" else "структура"
        self._строка(f"{ключ} {имя} {{")
        for c in d.get("inner", []):
            if c.get("kind") != "FieldDecl":
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
        if not имя:                             # анонимный enum — вынести как конст? пропуск
            self.добавить_пометку("проверка-транспилятора", d,
                                 деталь="анонимный enum — задайте имя или используйте «конст»")
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
        self.добавить_пометку("typedef-алиас", d,
                             деталь=f"«{имя}» = «{основа}» → «{конда_тип(qt)}»")
        self._строка("")

    def глобальная(self, d, ур=0):
        self.объявление(d, ур)

    def _указатель_декл_небезоп(self, n) -> bool:
        """Есть ли объявление указателя с небезопасным инициализатором —
        такое нельзя statement-обернуть (переменная уйдёт из области), значит
        всю функцию оборачиваем в «небезопасно { }»."""
        if not isinstance(n, dict) or "kind" not in n:
            return False
        if n["kind"] == "VarDecl":
            qt = без_квалификаторов(qualtype(n))
            вн = [c for c in n.get("inner", []) if isinstance(c, dict) and "kind" in c]
            иниц = вн[-1] if вн else None
            # исключаем malloc (он станет срезом) и адрес-оф (отдельный маркер)
            if "*" in qt and иниц is not None and self.небезопасен(иниц) \
                    and self._malloc_в_срез(n.get("name", ""), qualtype(n), иниц) is None \
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

    def функция(self, f):
        # Таблица владения ключуется ИСХОДНЫМ именем C — берём его до
        # переименования точки входа, иначе подстановки для main не найдутся.
        исходное_имя = f.get("name", "?")
        имя = "точка_входа" if исходное_имя == "main" else исходное_имя
        self.тип_возврата = конда_тип(qualtype(f).split("(")[0].strip())
        self.срез_переменные = set()
        self.ссылочные_имена = set()
        self.подстановки = self.владение.подстановки_функции(исходное_имя)
        self.тек_функция = имя
        тело = next((c for c in f.get("inner", []) if c.get("kind") == "CompoundStmt"), None)
        параметры = []
        индекс_п = -1
        for c in f.get("inner", []):
            if c.get("kind") != "ParmVarDecl":
                continue
            индекс_п += 1
            pимя = c.get("name", "_")
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
                         and self._имя_индексируется(pимя, тело)
                         and not self._имя_в_арифметике(pимя, тело))
            if режим and kt.endswith("*"):
                параметры.append(f"{режим} {kt[:-1]} {pимя}")
                self.ссылочные_имена.add(pимя)
            elif kt.endswith("*") and (от_цикла or эвристика):
                элем = kt[:-1]
                параметры.append(f"срез<{элем}> {pимя}")
                self.срез_переменные.add(pимя)
            else:
                параметры.append(f"{kt} {pимя}")
        if имя == "точка_входа" and not параметры:
            параметры = ["целое32 количество_аргументов", "символ** аргументы"]
        self._строка(f"{self.тип_возврата} {имя}(" + ", ".join(параметры) + ")")
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
            умолч = значение_по_умолчанию(self.тип_возврата)
            if умолч is not None:
                self.эмит(1, f"вернуть {умолч}")
            elif self.тип_возврата != "ничего":
                self.добавить_пометку("проверка-транспилятора", f,
                                     деталь="функция возвращает указатель из "
                                            "небезопасного тела — добавьте «вернуть»",
                                     ур=1)
        else:
            for c in тело.get("inner", []):
                self.оператор(c, 1)
        self._строка("}")
        self._строка("")


# ─── драйвер ─────────────────────────────────────────────────────────────────
def _завершён(операторы):
    """Заканчивается ли список операторов на break/return/continue."""
    for c in reversed(операторы):
        if c.get("kind") in ("BreakStmt", "ReturnStmt", "ContinueStmt"):
            return True
        if c.get("kind") in ("CaseStmt", "DefaultStmt"):
            continue
        return False
    return False


def дамп_clang(путь, доп):
    if not shutil.which("clang"):
        sys.stderr.write("ошибка: clang не найден в PATH\n")
        sys.exit(2)
    cmd = ["clang", "-Xclang", "-ast-dump=json", "-fsyntax-only", путь] + доп
    proc = subprocess.run(cmd, capture_output=True, text=True)
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


def сгенерировать(декларации, политика, исходник_c):
    """Один прогон эмиссии при заданной политике. → (текст, Конвертер)."""
    таблица_влад = влад.проанализировать(декларации, qualtype, без_квалификаторов,
                                         политика)
    к = Конвертер(политика, исходник_c, таблица_влад)
    for d in декларации:                       # 1-й проход: поля структур/union
        if d.get("kind") == "RecordDecl" and d.get("name"):
            к.регистрация_записи(d)
    заголовок = False
    for d in декларации:                       # 2-й проход: эмиссия
        k = d.get("kind")
        if k == "RecordDecl" and d.get("completeDefinition"):
            к.структура(d)
        elif k == "EnumDecl":
            к.перечисление(d)
        elif k == "TypedefDecl":
            к.typedef(d)
        elif k == "FunctionDecl":
            if any(a.get("kind") == "CompoundStmt" for a in d.get("inner", [])):
                заголовок = True
            к.функция(d)
        elif k == "VarDecl":
            к.глобальная(d)
    if заголовок:                              # шапка — в те же списки, иначе
        шапка = ["#содержит <stdio.h>", "#содержит <stdlib.h>", ""]  # съедут номера
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


def конвертировать(путь, доп, транспилятор=None, проверять=True, макс_итераций=6):
    """C → Konda. При включённой проверке гоняет цикл «сгенерировал → проверил
    транспилятором → уточнил», ослабляя безопасность только по фактам.
    → (текст, Конвертер, оставшиеся_диагностики, число_итераций)."""
    корень = дамп_clang(путь, доп)
    база = os.path.basename(путь)
    декларации = list(главные_объявления(корень, база))
    try:
        with open(путь, encoding="utf-8", errors="replace") as fh:
            исходник = fh.read().splitlines()
    except OSError:
        исходник = []
    нормализовать_позиции(декларации)
    индекс = индекс_узлов(декларации)
    функции = {д["name"]: д for д in декларации
               if д.get("kind") == "FunctionDecl" and д.get("name")}
    политика = Политика()
    текст, к = сгенерировать(декларации, политика, исходник)
    if not проверять or not транспилятор:
        return текст, к, [], 0

    for итерация in range(1, макс_итераций + 1):
        диаги = пров.прогнать(транспилятор, текст)
        if not диаги:
            return текст, к, [], итерация
        до = политика.отпечаток()
        применить_диагностики(диаги, к, политика, индекс, функции)
        if политика.отпечаток() == до:
            # новых исправлений нет — оставляем пометки на местах ошибок
            сдаться(диаги, к, политика)
            текст, к = сгенерировать(декларации, политика, исходник)
            return текст, к, диаги, итерация
        текст, к = сгенерировать(декларации, политика, исходник)

    диаги = пров.прогнать(транспилятор, текст)
    if диаги:
        сдаться(диаги, к, политика)
        текст, к = сгенерировать(декларации, политика, исходник)
    return текст, к, диаги, макс_итераций


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 3
    путь, вывод, отчёт, доп = argv[1], None, None, []
    проверять, макс_итераций, явный_тр = True, 6, None
    i = 2
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
        elif а == "--":
            доп = argv[i + 1:]; break
        else:
            i += 1
    if not os.path.exists(путь):
        sys.stderr.write(f"файл не найден: {путь}\n")
        return 3

    транспилятор = пров.найти_транспилятор(явный_тр) if проверять else None
    if проверять and not транспилятор:
        sys.stderr.write("предупреждение: транспилятор не найден — цикл проверки "
                         "выключен (укажите --транспилятор ПУТЬ или "
                         "KONDA_ТРАНСПИЛЯТОР)\n")
    текст, к, диаги, итераций = конвертировать(
        путь, доп, транспилятор, проверять and bool(транспилятор), макс_итераций)

    имя_вывода = вывод or "-"
    if вывод:
        with open(вывод, "w", encoding="utf-8") as fh:
            fh.write(текст)
    else:
        sys.stdout.write(текст)

    if отчёт:
        with open(отчёт, "w", encoding="utf-8") as fh:
            fh.write(пм.отчёт_json(к.пометки, путь, имя_вывода))

    # Сводка в stderr. Важная тонкость: непереводимый фрагмент уходит в
    # комментарий, поэтому остаток МОЖЕТ пройти транспилятор — но поведение
    # программы при этом молча изменится. Не называем это успехом.
    блок = sum(1 for п in к.пометки if п.категория in пм.БЛОКИРУЮЩИЕ)
    if not транспилятор:
        итог = "без проверки"
    elif диаги:
        итог = "НЕ проходит транспилятор"
    elif блок:
        итог = ("проходит транспилятор, НО поведение изменено "
                "(непереводимое закомментировано)")
    else:
        итог = "принят транспилятором"
    if вывод:
        sys.stderr.write(f"записано: {вывод}\n")
    sys.stderr.write(f"итог: {итог}")
    if итераций:
        sys.stderr.write(f" (итераций цикла: {итераций})")
    sys.stderr.write(f"; пометок: {len(к.пометки)}")
    if блок:
        sys.stderr.write(f" (из них блокируют компиляцию: {блок})")
    sys.stderr.write("\n")
    for кат in (пм.НЕПЕРЕВОДИМО, пм.ОШИБКА, пм.НЕБЕЗОПАСНО, пм.ПРОВЕРИТЬ):
        н = sum(1 for п in к.пометки if п.категория == кат)
        if н:
            sys.stderr.write(f"  {кат}: {н}\n")
    if отчёт:
        sys.stderr.write(f"отчёт для ИИ/трекера: {отчёт}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
