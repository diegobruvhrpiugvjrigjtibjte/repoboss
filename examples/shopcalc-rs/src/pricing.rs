//! Order total: line prices, bulk discount, shipping and tax.

use crate::catalog::price_of;
use crate::settings;

fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

pub fn line_total(sku: &str, quantity: u32) -> Result<f64, String> {
    if quantity == 0 {
        return Err("quantity must be positive".to_string());
    }
    let mut total = price_of(sku)? * f64::from(quantity);
    if quantity >= settings::BULK_MIN_QUANTITY {
        total *= 1.0 - settings::BULK_DISCOUNT;
    }
    Ok(round2(total))
}

pub fn subtotal(lines: &[(&str, u32)]) -> Result<f64, String> {
    let mut sum = 0.0;
    for (sku, quantity) in lines {
        sum += line_total(sku, *quantity)?;
    }
    Ok(round2(sum))
}

pub fn shipping(amount: f64) -> f64 {
    if amount == 0.0 {
        return 0.0;
    }
    if amount >= settings::FREE_SHIPPING_THRESHOLD {
        return 0.0;
    }
    settings::SHIPPING_FEE
}

pub fn tax(amount: f64) -> f64 {
    round2(amount * settings::TAX_RATE)
}

pub fn order_total(lines: &[(&str, u32)]) -> Result<f64, String> {
    let net = subtotal(lines)?;
    Ok(round2(net + tax(net) + shipping(net)))
}
