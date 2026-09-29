/* typedef скаляров из СИСТЕМНЫХ заголовков («time_t», «clockid_t», «pid_t»,
 * «ssize_t»…): транспилятор их имён не знает → kfc разворачивает typedef в
 * примитив Konda (через desugaredQualType clang). */
#include <stdio.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

static ssize_t удвоить(ssize_t n) { return n * 2; }

int main(void)
{
    time_t t = 7;
    clockid_t часы = CLOCK_MONOTONIC;
    pid_t п = 5;
    printf("%ld %d %d %ld\n", (long)t, (int)часы, (int)п, (long)удвоить(21));
    return 0;
}
