namespace Shopcalc;

/// <summary>Business rules, in one place.</summary>
public static class Settings
{
    public const decimal TAX_RATE = 0.22m;
    public const decimal FREE_SHIPPING_THRESHOLD = 50m;
    public const decimal SHIPPING_FEE = 4.9m;
    public const int BULK_MIN_QUANTITY = 10;
    public const decimal BULK_DISCOUNT = 0.1m;
}
