/* Product catalog backed by a CSV data file (prices in cents). */
#include <stdio.h>
#include <string.h>

#include "shopcalc.h"

#ifndef DATA_DIR
#define DATA_DIR "data"
#endif

static FILE *open_catalog(void) {
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", DATA_DIR, "catalog.csv");
    return fopen(path, "r");
}

static long lookup(const char *sku, int *count) {
    FILE *file = open_catalog();
    char line[256];
    long found = -1;
    *count = 0;
    if (file == NULL) {
        return -1;
    }
    if (fgets(line, sizeof line, file) == NULL) { /* header */
        fclose(file);
        return -1;
    }
    while (fgets(line, sizeof line, file) != NULL) {
        char name[64];
        long cents;
        if (sscanf(line, "%63[^,],%ld", name, &cents) == 2) {
            (*count)++;
            if (sku != NULL && strcmp(name, sku) == 0) {
                found = cents;
            }
        }
    }
    fclose(file);
    return found;
}

long price_of(const char *sku) {
    int count;
    return lookup(sku, &count);
}

int catalog_size(void) {
    int count;
    lookup(NULL, &count);
    return count;
}
