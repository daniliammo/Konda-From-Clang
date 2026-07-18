# -*- coding: utf-8 -*-
"""
NULL-анализ: какие указатели могут быть «нуль» → «возможно<T*>».

В Konda обычный «T*» ненулевой по умолчанию: ему нельзя присвоить «нуль» и
нельзя объявить без инициализатора. Nullable-указатель — только «возможно<T*>»,
разворот под охранником «если (п != нуль)» / «если (п == нуль) … иначе».

C-код различия не знает: NULL присваивают, возвращают как «не нашлось»,
сравнивают в охранниках. Этот модуль находит такие указатели, чтобы эмиттер
объявил их «возможно<T*>», а NULL печатал как «нуль».

Свидетельства nullable для указателя «p» (параметра или локальной):
  * инициализатор/присваивание NULL;
  * сравнение с NULL («p == NULL», «p != NULL») или проверка истинности
    («if (p)», «if (!p)») — в C так пишут именно про возможно-нулевые;
  * инициализация/присваивание от вызова функции, возвращающей nullable;
  * получение аргументом позиции, куда кто-то передаёт nullable/NULL.
Функция возвращает nullable, если хоть один «вернуть» отдаёт NULL или
nullable-переменную. Свойства взаимно рекурсивны → фикспоинт (наименьшая
неподвижная точка: начинаем с прямых свидетельств, распространяем до
стабилизации).

Свидетельства делятся на СИЛЬНЫЕ (NULL реально присваивается/возвращается —
без «возможно» код не скомпилируется вовсе) и СЛАБЫЕ (только сравнение — код
и так бы прошёл). Цикл проверки может ОТМЕНИТЬ слабое свидетельство
(`политика.отмена_возможно`), если «возможно» породило новые ошибки, и
ВКЛЮЧИТЬ nullable принудительно (`политика.включить_возможно`) по диагностике
«указатель ненулевой по умолчанию …».
"""


def _снять_скобки(n):
    while isinstance(n, dict) and n.get("kind") in (
            "ParenExpr", "ConstantExpr", "ExprWithCleanups", "FullExpr"):
        вн = n.get("inner", [])
        if not вн:
            break
        n = вн[-1]
    return n


def _качтип(n):
    t = n.get("type")
    if isinstance(t, dict):
        return t.get("qualType", "")
    return t or ""


def _целочисленный_нуль(n):
    n = _снять_скобки(n)
    if not isinstance(n, dict):
        return False
    if n.get("kind") == "IntegerLiteral":
        return n.get("value") == "0"
    if n.get("kind") in ("ImplicitCastExpr", "CStyleCastExpr"):
        вн = n.get("inner", [])
        return bool(вн) and _целочисленный_нуль(вн[0])
    return False


def это_нуль(n):
    """Выражение — нулевой указатель: NULL, (void*)0, (T*)NULL, nullptr."""
    n = _снять_скобки(n)
    if not isinstance(n, dict):
        return False
    k = n.get("kind")
    if k in ("GNUNullExpr", "CXXNullPtrLiteralExpr"):
        return True
    if k == "ImplicitCastExpr" and n.get("castKind") in ("NullToPointer",):
        return True
    if k in ("ImplicitCastExpr", "CStyleCastExpr") and "*" in _качтип(n):
        вн = n.get("inner", [])
        return bool(вн) and (это_нуль(вн[0]) or _целочисленный_нуль(вн[0]))
    return False


def _имя_ссылки(n):
    n = _снять_скобки(n)
    while isinstance(n, dict) and n.get("kind") == "ImplicitCastExpr":
        вн = n.get("inner", [])
        if not вн:
            break
        n = _снять_скобки(вн[0])
    if isinstance(n, dict) and n.get("kind") == "DeclRefExpr":
        return (n.get("referencedDecl") or {}).get("name")
    return None


class НулевыеУказатели:
    """Результат анализа. Имена — в пределах своей функции."""

    def __init__(self):
        self.имена = {}       # функция → {имя nullable-указателя (парам/локал)}
        self.сильные = set()  # (функция, имя) — NULL присваивается, отменять нельзя
        self.возвраты = set()  # функции, возвращающие nullable-указатель

    def нулевой(self, функция, имя):
        return имя in self.имена.get(функция, set())


def _одиночный_указатель(qt):
    return qt.count("*") == 1 and "[" not in qt and "(" not in qt


def проанализировать(декларации, qualtype, без_квалификаторов, политика=None):
    """→ НулевыеУказатели. Фикспоинт по свидетельствам (см. шапку модуля)."""
    отмена = getattr(политика, "отмена_возможно", set()) if политика else set()
    принуд = getattr(политика, "включить_возможно", set()) if политика else set()

    рез = НулевыеУказатели()
    тела = {}       # функция → тело
    указатели = {}  # функция → {имя: True} — одиночные T*-параметры и локальные
    for д in декларации:
        if д.get("kind") != "FunctionDecl":
            continue
        имя_ф = д.get("name")
        тело = next((c for c in д.get("inner", [])
                     if c.get("kind") == "CompoundStmt"), None)
        if тело is None or not имя_ф:
            continue
        тела[имя_ф] = д
        мест = {}
        for c in д.get("inner", []):
            if c.get("kind") == "ParmVarDecl" and c.get("name") and \
                    _одиночный_указатель(без_квалификаторов(qualtype(c))):
                мест[c["name"]] = True

        def локалы(n):
            if not isinstance(n, dict):
                return
            if n.get("kind") == "VarDecl" and n.get("name") and \
                    _одиночный_указатель(без_квалификаторов(qualtype(n))):
                мест[n["name"]] = True
            for в in n.get("inner", []):
                локалы(в)
        локалы(тело)
        указатели[имя_ф] = мест

    нулевые = {ф: set() for ф in тела}          # рабочее множество имён
    сильные = set()

    def пометить(ф, имя, сильное):
        if имя not in указатели.get(ф, {}):
            return False
        if сильное:
            сильные.add((ф, имя))
        elif (ф, имя) in отмена:
            return False                        # слабое свидетельство отменено
        if имя in нулевые[ф]:
            return False
        нулевые[ф].add(имя)
        return True

    # принудительные включения от цикла проверки — как сильные свидетельства
    for ф, имя in принуд:
        if ф in нулевые:
            пометить(ф, имя, True)

    менялось = True
    while менялось:
        менялось = False
        for имя_ф, д in тела.items():
            тело = next(c for c in д.get("inner", [])
                        if c.get("kind") == "CompoundStmt")

            def обход(n):
                nonlocal менялось
                if not isinstance(n, dict) or "kind" not in n:
                    return
                k = n.get("kind")
                вн = n.get("inner", [])
                # объявление с инициализатором
                if k == "VarDecl" and n.get("name"):
                    иниц = [c for c in вн if isinstance(c, dict) and "kind" in c]
                    if иниц:
                        и = иниц[-1]
                        if это_нуль(и):
                            менялось |= пометить(имя_ф, n["name"], True)
                        else:
                            ф_вызв = _имя_вызова(и)
                            if ф_вызв in рез.возвраты:
                                менялось |= пометить(имя_ф, n["name"], True)
                            имя_ист = _имя_ссылки(и)
                            if имя_ист and имя_ист in нулевые[имя_ф]:
                                менялось |= пометить(имя_ф, n["name"], True)
                # присваивание
                if k == "BinaryOperator" and n.get("opcode") == "=" and len(вн) >= 2:
                    цель = _имя_ссылки(вн[0])
                    if цель:
                        if это_нуль(вн[1]):
                            менялось |= пометить(имя_ф, цель, True)
                        else:
                            ф_вызв = _имя_вызова(вн[1])
                            if ф_вызв in рез.возвраты:
                                менялось |= пометить(имя_ф, цель, True)
                            имя_ист = _имя_ссылки(вн[1])
                            if имя_ист and имя_ист in нулевые[имя_ф]:
                                менялось |= пометить(имя_ф, цель, True)
                # сравнение с NULL — слабое свидетельство
                if k == "BinaryOperator" and n.get("opcode") in ("==", "!=") \
                        and len(вн) >= 2:
                    for а, б in ((вн[0], вн[1]), (вн[1], вн[0])):
                        имя = _имя_ссылки(а)
                        if имя and это_нуль(б):
                            менялось |= пометить(имя_ф, имя, False)
                # Проверка истинности указателя — слабое свидетельство.
                # clang в режиме C НЕ ставит PointerToBoolean, поэтому смотрим
                # булевы контексты и ТИП операнда: «if (p)», «while (p)»,
                # «!p», «p && …».
                кандидаты = []
                if k in ("IfStmt", "WhileStmt") and вн:
                    кандидаты.append(вн[0])
                if k == "DoStmt" and вн:
                    кандидаты.append(вн[-1])
                if k == "UnaryOperator" and n.get("opcode") == "!" and вн:
                    кандидаты.append(вн[0])
                if k == "BinaryOperator" and n.get("opcode") in ("&&", "||"):
                    кандидаты.extend(вн[:2])
                for у in кандидаты:
                    имя = _имя_ссылки(у)
                    if имя and "*" in _качтип(_снять_скобки(у)):
                        менялось |= пометить(имя_ф, имя, False)
                # возврат NULL / nullable-имени → функция возвращает nullable
                if k == "ReturnStmt" and вн:
                    в = вн[0]
                    возвр_нул = это_нуль(в)
                    имя = _имя_ссылки(в)
                    if имя and имя in нулевые[имя_ф]:
                        возвр_нул = True
                    ф_вызв = _имя_вызова(в)
                    if ф_вызв in рез.возвраты:
                        возвр_нул = True
                    if возвр_нул and имя_ф not in рез.возвраты:
                        рез.возвраты.add(имя_ф)
                        менялось = True
                for c in вн:
                    обход(c)
            обход(тело)

    рез.имена = {ф: имена for ф, имена in нулевые.items() if имена}
    рез.сильные = сильные
    return рез


def _имя_вызова(n):
    """«f(...)» → имя f, иначе None (сквозь касты/скобки)."""
    n = _снять_скобки(n)
    while isinstance(n, dict) and n.get("kind") == "ImplicitCastExpr":
        вн = n.get("inner", [])
        if not вн:
            break
        n = _снять_скобки(вн[0])
    if isinstance(n, dict) and n.get("kind") == "CallExpr":
        вн = n.get("inner", [])
        if вн:
            return _имя_ссылки(вн[0])
    return None
