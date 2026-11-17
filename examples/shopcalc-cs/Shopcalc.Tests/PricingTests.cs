using Shopcalc;
using Xunit;

public class PricingTests
{
    [Fact]
    public void CatalogLoadsAllProducts()
    {
        var catalog = Catalog.Load();
        Assert.Equal(4, catalog.Count);
        Assert.Equal(20m, catalog["LAMP"]);
    }

    [Fact]
    public void UnknownProductIsRejected() => Assert.Throws<KeyNotFoundException>(() => Catalog.PriceOf("UMBRELLA"));

    [Fact]
    public void LineTotalWithoutDiscount() => Assert.Equal(12m, Pricing.LineTotal("NOTEBOOK", 3));

    [Fact]
    public void BulkDiscountStartsExactlyAtTheThreshold()
    {
        Assert.Equal(13.5m, Pricing.LineTotal("PEN", 9));
        Assert.Equal(13.5m, Pricing.LineTotal("PEN", 10));
    }

    [Fact]
    public void BulkDiscountRate() => Assert.Equal(72m, Pricing.LineTotal("NOTEBOOK", 20));

    [Fact]
    public void QuantityMustBePositive() =>
        Assert.Throws<ArgumentOutOfRangeException>(() => Pricing.LineTotal("PEN", 0));

    [Fact]
    public void ShippingIsChargedBelowThreshold() => Assert.Equal(4.9m, Pricing.Shipping(49.99m));

    [Fact]
    public void ShippingIsFreeAtAndAboveThreshold()
    {
        Assert.Equal(0m, Pricing.Shipping(50m));
        Assert.Equal(0m, Pricing.Shipping(120m));
    }

    [Fact]
    public void EmptyOrderHasNoShipping() => Assert.Equal(0m, Pricing.Shipping(0m));

    [Fact]
    public void TaxRate()
    {
        Assert.Equal(0.22m, Settings.TAX_RATE);
        Assert.Equal(22m, Pricing.Tax(100m));
    }

    [Fact]
    public void SmallOrderTotal() => Assert.Equal(14.66m, Pricing.OrderTotal(new[] { ("NOTEBOOK", 2) }));

    [Fact]
    public void LargeOrderTotal() =>
        Assert.Equal(67.1m, Pricing.OrderTotal(new[] { ("BACKPACK", 1), ("LAMP", 1) }));

    [Fact]
    public void SubtotalOfMixedLines() => Assert.Equal(53.5m, Pricing.Subtotal(new[] { ("PEN", 10), ("LAMP", 2) }));
}
