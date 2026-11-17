/* Order total: line prices, bulk discount, shipping and tax (all in cents). */
#include "shopcalc.h"

long line_total(const char *sku, int quantity) {
    long price;
    long total;
    if (quantity <= 0) {
        return -1;
    }
    price = price_of(sku);
    if (price < 0) {
        return -1;
    }
    total = price * quantity;
    if (quantity >= BULK_MIN_QUANTITY) {
        total = total * (100 - BULK_DISCOUNT_PERCENT) / 100;
    }
    return total;
}

long shipping(long amount) {
    if (amount == 0) {
        return 0;
    }
    if (amount >= FREE_SHIPPING_THRESHOLD) {
        return 0;
    }
    return SHIPPING_FEE;
}

long tax(long amount) {
    return (amount * TAX_RATE_PERCENT + 50) / 100;
}
