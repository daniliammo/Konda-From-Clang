#include <stdio.h>
struct рес { int busy; };
static void закрыть(struct рес *р, int код) { р->busy = код; }
int настроить(struct рес *р, int режим)
{
    int код_ошибки = -99;
    р->busy = 1;
    if (режим < 0)
        goto ошибка;
    if (режим > 100)
        goto ошибка;
    printf("режим=%d\n", режим);
    return 0;
ошибка:
    закрыть(р, код_ошибки);
    return код_ошибки;
}
int main(void) { struct рес р = {0}; int rc = настроить(&р, 5); printf("rc=%d busy=%d\n", rc, р.busy); return 0; }
