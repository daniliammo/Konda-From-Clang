/* «goto» — выход из вложенных циклов и переход к следующей итерации внешнего
 * → помеченные «прервать»/«продолжить» (§122 транспилятора). */
#include <stdio.h>

static int найти(int n)
{
	int r = -1;
	for (int i = 0; i < n; i++) {
		for (int j = 0; j < n; j++) {
			if (i * j == 6) {
				r = i * 10 + j;
				goto найдено;
			}
		}
	}
найдено:
	return r;
}

static int посчитать(int n)
{
	int шаги = 0;
	int i = 0;
	while (i < n) {
		i++;
		for (int j = 0; j < n; j++) {
			шаги++;
			if (j == 1)
				goto дальше;
		}
		шаги += 100;           /* не выполняется: goto всегда раньше */
дальше:
		;
	}
	return шаги;
}

int main(void)
{
	printf("%d %d\n", найти(5), посчитать(3));
	return 0;
}
