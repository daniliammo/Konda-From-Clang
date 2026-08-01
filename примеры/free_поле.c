#include <stdlib.h>
struct w { int *buffers; };
static void уничтожить(struct w *w)
{
    free(w->buffers);
    free(w);
}
int main(void) { return 0; }
