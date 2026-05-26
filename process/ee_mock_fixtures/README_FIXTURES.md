# Real GetProductMaster payloads — Stage 2 fixtures

These are REAL captured EasyEcom GetProductMaster responses (from test accounts).
The field SHAPES are ground truth; reconcile the EasyEcom-Item-Pull ruleset against these.
Drop into: apps/ecommerce_super/<app>/tests/ee_mock/ (or wherever ee fixtures live).

## getproductmaster_response.json
A 3-product page exercising the main branches + the real "dirt":
- mob000 (normal_product): accounting_unit="111" (dirty UOM), EANUPC="testUomcode001" (garbage EAN), tax_rule_name="tax_28%" tax_rate=0.28, hsn 8517. → creates an Item.
- Top3 / shirt000 (variant_parent): → FLAG-NOT-CREATED (unsupported type). Note tax_rule_name="TAx Rule5" tax_rate=0.03.
- Nike / shirt111 (combo_product) with sub_products[shirt000_Red_S]: → Stage 4 (flag/skip in Stage 2). Note SAME rule "TAx Rule5" resolves to tax_rate=0 here (rate variance on same rule name — confirms we map on resolved rate/rule per 8c, never parse the name).
- nextUrl carries a real cursor (for the resumable cursor-walk).

## getproductmaster_child_product.json
A real child_product (SKINQ serum) → FLAG-NOT-CREATED (unsupported type). Clean fields (real production-grade: hsn 33049910, accounting_unit "PCS", tax_rule_name "GST-18", cess present). nextUrl=null (last page).

## Notes for reconciliation
- product_type is a STRING in responses (normal_product / variant_parent / child_product / combo_product).
- accounting_unit is the UOM signal but dirty ("111","333","11","PCS") → default-UOM transform.
- EANUPC can be garbage ("testUomcode001") → not used for matching.
- cp_inventory ignorable; product_id/cp_id internal (store for push, not the join key — sku is).
- "product shelf life" key has a SPACE (the §5 path validator already tolerates space-bearing keys — 8a fix).
- cess only present on some products (child line has it; the 3-product page omits it on the parents).
- Expect ONE more reconciliation pass when real PRODUCTION payloads flow; these are test-account shape.
