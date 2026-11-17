namespace Shopcalc;

/// <summary>Order total: line prices, bulk discount, shipping and tax.</summary>
public static class Pricing
{
    public static decimal LineTotal(string sku, int quantity)
    {
        if (quantity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(quantity), "quantity must be positive");
        }
        var total = Catalog.PriceOf(sku) * quantity;
        if (quantity >= Settings.BULK_MIN_QUANTITY)
        {
            total *= 1 - Settings.BULK_DISCOUNT;
        }
        return Math.Round(total, 2);
    }

    public static decimal Subtotal(IEnumerable<(string Sku, int Quantity)> lines) =>
        Math.Round(lines.Sum(line => LineTotal(line.Sku, line.Quantity)), 2);

    public static decimal Shipping(decimal amount)
    {
        if (amount == 0)
        {
            return 0;
        }
        if (amount >= Settings.FREE_SHIPPING_THRESHOLD)
        {
            return 0;
        }
        return Settings.SHIPPING_FEE;
    }

    public static decimal Tax(decimal amount) => Math.Round(amount * Settings.TAX_RATE, 2);

    public static decimal OrderTotal(IEnumerable<(string Sku, int Quantity)> lines)
    {
        var net = Subtotal(lines);
        return Math.Round(net + Tax(net) + Shipping(net), 2);
    }
}
