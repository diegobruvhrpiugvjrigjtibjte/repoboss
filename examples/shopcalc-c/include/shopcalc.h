/* Order pricing (C port of the RepoBoss demo repository). Amounts are in cents. */
#ifndef SHOPCALC_H
#define SHOPCALC_H

#define TAX_RATE_PERCENT 22
#define FREE_SHIPPING_THRESHOLD 5000
#define SHIPPING_FEE 490
#define BULK_MIN_QUANTITY 10
#define BULK_DISCOUNT_PERCENT 10

/* Price in cents, or -1 when the product is unknown or the catalog cannot be read. */
long price_of(const char *sku);
int catalog_size(void);

/* Line total in cents, or -1 for invalid input. */
long line_total(const char *sku, int quantity);
long shipping(long amount);
long tax(long amount);

#endif
