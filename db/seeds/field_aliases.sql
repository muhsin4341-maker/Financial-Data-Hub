-- Field alias mapping: common label variants and XBRL us-gaap concept names → canonical_field_key.
-- Engineering Spec Part 1, Section 1.3 / Part 2, Section 5.4 (XBRL Extraction).
-- Expand this table as new companies and reporting variants are encountered.

-- ── Revenue aliases ───────────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('revenue', 'Net revenues',                       'label', NULL),
('revenue', 'Net sales',                          'label', NULL),
('revenue', 'Total revenues',                     'label', NULL),
('revenue', 'Total net revenues',                 'label', NULL),
('revenue', 'Revenues',                           'label', NULL),
('revenue', 'Revenue',                            'label', NULL),
('revenue', 'Turnover',                           'label', 'IFRS'),
('revenue', 'Net turnover',                       'label', 'IFRS'),
('revenue', 'us-gaap:Revenues',                   'xbrl',  'US_GAAP'),
('revenue', 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'xbrl', 'US_GAAP'),
('revenue', 'us-gaap:SalesRevenueNet',            'xbrl',  'US_GAAP'),
('revenue', 'us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax', 'xbrl', 'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Cost of revenue aliases ───────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('cost_of_revenue', 'Cost of goods sold',         'label', NULL),
('cost_of_revenue', 'Cost of sales',              'label', NULL),
('cost_of_revenue', 'Cost of revenue',            'label', NULL),
('cost_of_revenue', 'Cost of products',           'label', NULL),
('cost_of_revenue', 'us-gaap:CostOfRevenue',      'xbrl',  'US_GAAP'),
('cost_of_revenue', 'us-gaap:CostOfGoodsAndServicesSold', 'xbrl', 'US_GAAP'),
('cost_of_revenue', 'us-gaap:CostOfGoodsSold',   'xbrl',  'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Net income aliases ────────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('net_income', 'Net income',                      'label', NULL),
('net_income', 'Net earnings',                    'label', NULL),
('net_income', 'Profit for the year',             'label', 'IFRS'),
('net_income', 'Net profit',                      'label', NULL),
('net_income', 'us-gaap:NetIncomeLoss',           'xbrl',  'US_GAAP'),
('net_income', 'us-gaap:ProfitLoss',              'xbrl',  'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Net income (parent) aliases ───────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('net_income_attributable_to_parent', 'Net income attributable to common stockholders', 'label', NULL),
('net_income_attributable_to_parent', 'Net income attributable to Apple Inc.', 'label', NULL),
('net_income_attributable_to_parent', 'us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic', 'xbrl', 'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Total assets aliases ──────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('total_assets', 'Total assets',                  'label', NULL),
('total_assets', 'Total Assets',                  'label', NULL),
('total_assets', 'us-gaap:Assets',                'xbrl',  'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Total liabilities aliases ─────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('total_liabilities', 'Total liabilities',        'label', NULL),
('total_liabilities', 'us-gaap:Liabilities',      'xbrl',  'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Total equity aliases ──────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('total_equity', 'Total stockholders equity',         'label', NULL),
('total_equity', 'Total shareholders equity',         'label', NULL),
('total_equity', 'Total shareholders'' equity',        'label', NULL),
('total_equity', 'us-gaap:StockholdersEquity',        'xbrl',  'US_GAAP'),
('total_equity', 'us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', 'xbrl', 'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Operating cash flow aliases ───────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('net_cash_from_operations', 'Net cash generated by operating activities',   'label', 'IFRS'),
('net_cash_from_operations', 'Net cash provided by operating activities',    'label', 'US_GAAP'),
('net_cash_from_operations', 'Cash flows from operations',                   'label', NULL),
('net_cash_from_operations', 'us-gaap:NetCashProvidedByUsedInOperatingActivities', 'xbrl', 'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── Capex aliases ────────────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('capital_expenditures', 'Capital expenditures',                             'label', NULL),
('capital_expenditures', 'Purchases of property, plant and equipment',      'label', NULL),
('capital_expenditures', 'Additions to property, plant and equipment',      'label', NULL),
('capital_expenditures', 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'xbrl', 'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;

-- ── EPS aliases ──────────────────────────────────────────────────────────────
INSERT INTO field_aliases (canonical_field_key, alias, alias_type, reporting_standard) VALUES
('diluted_eps', 'Diluted earnings per share',                                'label', NULL),
('diluted_eps', 'Net income per diluted share',                              'label', NULL),
('diluted_eps', 'us-gaap:EarningsPerShareDiluted',                          'xbrl',  'US_GAAP'),
('basic_eps',   'Basic earnings per share',                                  'label', NULL),
('basic_eps',   'us-gaap:EarningsPerShareBasic',                             'xbrl',  'US_GAAP')
ON CONFLICT ON CONSTRAINT uq_field_aliases_key_alias_standard DO NOTHING;
