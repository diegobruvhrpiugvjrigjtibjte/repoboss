//! Product catalog backed by a CSV data file shipped with the crate.

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

fn data_file() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data").join("catalog.csv")
}

pub fn load_catalog() -> Result<HashMap<String, f64>, String> {
    let text = fs::read_to_string(data_file()).map_err(|e| format!("cannot read catalog: {e}"))?;
    let mut catalog = HashMap::new();
    for line in text.lines().skip(1) {
        let mut parts = line.split(',');
        if let (Some(sku), Some(price)) = (parts.next(), parts.next()) {
            let price: f64 = price.trim().parse().map_err(|_| format!("bad price for {sku}"))?;
            catalog.insert(sku.trim().to_string(), price);
        }
    }
    Ok(catalog)
}

pub fn price_of(sku: &str) -> Result<f64, String> {
    load_catalog()?.get(sku).copied().ok_or_else(|| format!("unknown product: {sku}"))
}
