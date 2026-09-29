#include <stdio.h>
#include "вкл/либа.h"

struct buffer { int busy; struct listener l; };

/* Слушатель берём у ДРУГОГО буфера: «&buf.l» + «&buf» в одном вызове —
 * алиасинг чтение+изменяемый одной переменной, его транспилятор запрещает
 * (§79, иначе авто-restrict некорректен). Цель примера — проекция
 * «&x.поле» → «чтение» и типизация void* по аргументу, не алиасинг. */
int main(void)
{
    int obj = 7;
    struct buffer buf, источник;
    buf.busy = 0;
    источник.l.tag = 5;
    add_listener(&obj, &источник.l, &buf);
    printf("%d %d\n", obj, источник.l.tag);
    return 0;
}
