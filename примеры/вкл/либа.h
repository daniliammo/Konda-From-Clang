#ifndef ЛИБА_H
#define ЛИБА_H
struct listener { int tag; };
static inline void add_listener(int *obj, const struct listener *lis, void *data) {
    (void)obj; (void)lis; (void)data;
}
#endif
