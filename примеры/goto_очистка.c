#include <stdio.h>

struct ресурс { int занят; };

static void закрыть(struct ресурс *р) { р->занят = 0; }

int настроить(struct ресурс *р, int режим)
{
    р->занят = 1;
    if (режим < 0)
        goto ошибка;
    if (режим > 100)
        goto ошибка;
    printf("режим=%d\n", режим);
    return 0;
ошибка:
    закрыть(р);
    return -1;
}

int main(void)
{
    struct ресурс р;
    р.занят = 0;
    int rc = настроить(&р, 5);
    printf("rc=%d занят=%d\n", rc, р.занят);
    return 0;
}
