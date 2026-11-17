using System.Globalization;

namespace Shopcalc;

/// <summary>Product catalog backed by a CSV data file copied next to the assembly.</summary>
public static class Catalog
{
    public static Dictionary<string, decimal> Load()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "Data", "catalog.csv");
        var catalog = new Dictionary<string, decimal>();
        foreach (var line in File.ReadAllLines(path).Skip(1))
        {
            var parts = line.Split(',');
            if (parts.Length == 2)
            {
                catalog[parts[0].Trim()] = decimal.Parse(parts[1].Trim(), CultureInfo.InvariantCulture);
            }
        }
        return catalog;
    }

    public static decimal PriceOf(string sku)
    {
        var catalog = Load();
        if (!catalog.TryGetValue(sku, out var price))
        {
            throw new KeyNotFoundException($"unknown product: {sku}");
        }
        return price;
    }
}
