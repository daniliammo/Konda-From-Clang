#ifndef КОНФИГ_H
#define КОНФИГ_H
struct config;
int config_get_string(struct config *c, const char *key, char **value, const char *def);
#endif
