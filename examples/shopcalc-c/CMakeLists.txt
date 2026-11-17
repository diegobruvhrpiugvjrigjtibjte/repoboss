cmake_minimum_required(VERSION 3.16)
project(shopcalc C)

set(CMAKE_C_STANDARD 11)

add_library(shopcalc src/catalog.c src/pricing.c)
target_include_directories(shopcalc PUBLIC include)
target_compile_definitions(shopcalc PRIVATE DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/data")

enable_testing()
add_executable(test_pricing tests/test_pricing.c)
target_link_libraries(test_pricing shopcalc)

foreach(name catalog_loads unknown_product line_total bulk_threshold bulk_rate quantity_positive
             shipping_below shipping_free empty_order tax_rate small_order large_order mixed_subtotal)
  add_test(NAME ${name} COMMAND test_pricing ${name})
endforeach()
