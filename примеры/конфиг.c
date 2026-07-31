#include <stdio.h>
#include "вкл/конфиг.h"
int main(void)
{
    struct config *c = 0;
    char *out = 0;
    config_get_string(c, "ключ", &out, "по умолчанию");
    printf("%p\n", (void*)out);
    return 0;
}
