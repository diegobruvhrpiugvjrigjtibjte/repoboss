use shopcalc::catalog::{load_catalog, price_of};
use shopcalc::pricing::{line_total, order_total, shipping, subtotal, tax};
use shopcalc::settings::TAX_RATE;

#[test]
fn catalog_loads_all_products() {
    let catalog = load_catalog().unwrap();
    assert_eq!(catalog.len(), 4);
    assert_eq!(catalog["LAMP"], 20.0);
}

#[test]
fn unknown_product_is_rejected() {
    assert!(price_of("UMBRELLA").is_err());
}

#[test]
fn line_total_without_discount() {
    assert_eq!(line_total("NOTEBOOK", 3).unwrap(), 12.0);
}

#[test]
fn bulk_discount_starts_exactly_at_the_threshold() {
    assert_eq!(line_total("PEN", 9).unwrap(), 13.5);
    assert_eq!(line_total("PEN", 10).unwrap(), 13.5);
}

#[test]
fn bulk_discount_rate() {
    assert_eq!(line_total("NOTEBOOK", 20).unwrap(), 72.0);
}

#[test]
fn quantity_must_be_positive() {
    assert!(line_total("PEN", 0).is_err());
}

#[test]
fn shipping_is_charged_below_threshold() {
    assert_eq!(shipping(49.99), 4.9);
}

#[test]
fn shipping_is_free_at_and_above_threshold() {
    assert_eq!(shipping(50.0), 0.0);
    assert_eq!(shipping(120.0), 0.0);
}

#[test]
fn empty_order_has_no_shipping() {
    assert_eq!(shipping(0.0), 0.0);
}

#[test]
fn tax_rate() {
    assert_eq!(TAX_RATE, 0.22);
    assert_eq!(tax(100.0), 22.0);
}

#[test]
fn small_order_total() {
    assert_eq!(order_total(&[("NOTEBOOK", 2)]).unwrap(), 14.66);
}

#[test]
fn large_order_total() {
    assert_eq!(order_total(&[("BACKPACK", 1), ("LAMP", 1)]).unwrap(), 67.1);
}

#[test]
fn subtotal_of_mixed_lines() {
    assert_eq!(subtotal(&[("PEN", 10), ("LAMP", 2)]).unwrap(), 53.5);
}
