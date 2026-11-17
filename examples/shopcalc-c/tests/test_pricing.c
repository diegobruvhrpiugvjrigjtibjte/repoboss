/* Each test is selected by name on the command line; ctest registers one entry per test. */
#include <stdio.h>
#include <string.h>

#include "shopcalc.h"

static int failures = 0;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                   \
        }                                                                 \
    } while (0)

static long order_total(const char **skus, const int *quantities, int n) {
    long net = 0;
    for (int i = 0; i < n; i++) {
        net += line_total(skus[i], quantities[i]);
    }
    return net + tax(net) + shipping(net);
}

int main(int argc, char **argv) {
    const char *name = argc > 1 ? argv[1] : "";
    if (strcmp(name, "catalog_loads") == 0) {
        CHECK(catalog_size() == 4);
        CHECK(price_of("LAMP") == 2000);
    } else if (strcmp(name, "unknown_product") == 0) {
        CHECK(price_of("UMBRELLA") == -1);
    } else if (strcmp(name, "line_total") == 0) {
        CHECK(line_total("NOTEBOOK", 3) == 1200);
    } else if (strcmp(name, "bulk_threshold") == 0) {
        CHECK(line_total("PEN", 9) == 1350);
        CHECK(line_total("PEN", 10) == 1350);
    } else if (strcmp(name, "bulk_rate") == 0) {
        CHECK(line_total("NOTEBOOK", 20) == 7200);
    } else if (strcmp(name, "quantity_positive") == 0) {
        CHECK(line_total("PEN", 0) == -1);
    } else if (strcmp(name, "shipping_below") == 0) {
        CHECK(shipping(4999) == 490);
    } else if (strcmp(name, "shipping_free") == 0) {
        CHECK(shipping(5000) == 0);
        CHECK(shipping(12000) == 0);
    } else if (strcmp(name, "empty_order") == 0) {
        CHECK(shipping(0) == 0);
    } else if (strcmp(name, "tax_rate") == 0) {
        CHECK(tax(10000) == 2200);
    } else if (strcmp(name, "small_order") == 0) {
        const char *skus[] = {"NOTEBOOK"};
        const int qty[] = {2};
        CHECK(order_total(skus, qty, 1) == 1466);
    } else if (strcmp(name, "large_order") == 0) {
        const char *skus[] = {"BACKPACK", "LAMP"};
        const int qty[] = {1, 1};
        CHECK(order_total(skus, qty, 2) == 6710);
    } else if (strcmp(name, "mixed_subtotal") == 0) {
        CHECK(line_total("PEN", 10) + line_total("LAMP", 2) == 5350);
    } else {
        fprintf(stderr, "unknown test %s\n", name);
        return 2;
    }
    return failures == 0 ? 0 : 1;
}
