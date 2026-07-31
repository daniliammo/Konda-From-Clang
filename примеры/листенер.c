#include <stdio.h>
#include "вкл/либа.h"

struct buffer { int busy; struct listener l; };

int main(void)
{
    int obj = 7;
    struct buffer buf;
    buf.busy = 0;
    buf.l.tag = 5;
    add_listener(&obj, &buf.l, &buf);
    printf("%d %d\n", obj, buf.l.tag);
    return 0;
}
