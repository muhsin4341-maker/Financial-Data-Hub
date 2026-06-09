-- Canonical field definitions — source of truth for all extraction and export.
-- Engineering Spec Part 2, Section 5.3.
-- Idempotent: safe to re-run; duplicate field_keys are silently skipped.

-- ── Income Statement (22 fields) ─────────────────────────────────────────────
INSERT INTO canonical_fields (field_key, display_name, statement_type, section, sign_convention, is_required, description) VALUES
('revenue',                         'Revenue',                              'income', 'revenue',     1,  true,  'Total net revenues / net sales'),
('cost_of_revenue',                  'Cost of Revenue',                      'income', 'cogs',        1,  true,  'Cost of goods sold / cost of sales'),
('gross_profit',                     'Gross Profit',                         'income', 'profit',      1,  true,  'Revenue minus cost of revenue (computed check)'),
('research_and_development',         'Research & Development',               'income', 'opex',        1,  false, 'R&D expense'),
('sales_general_administrative',     'SG&A',                                 'income', 'opex',        1,  true,  'Selling, general and administrative expenses'),
('total_operating_expenses',         'Total Operating Expenses',             'income', 'opex',        1,  true,  'All operating expense line items summed'),
('operating_income',                 'Operating Income (EBIT)',               'income', 'profit',      1,  true,  'Gross profit minus total operating expenses'),
('interest_expense',                 'Interest Expense',                     'income', 'non_op',      1,  true,  'Interest costs on debt obligations'),
('interest_income',                  'Interest Income',                      'income', 'non_op',      1,  false, 'Interest earned on cash and investments'),
('other_income_expense',             'Other Income / (Expense)',             'income', 'non_op',      1,  false, 'Non-operating items net'),
('income_before_tax',                'Income Before Tax',                    'income', 'profit',      1,  true,  'Pre-tax profit (EBT)'),
('income_tax_expense',               'Income Tax Expense',                   'income', 'tax',         1,  true,  'Total income tax provision'),
('net_income',                       'Net Income',                           'income', 'profit',      1,  true,  'Bottom-line profit including minority interest'),
('net_income_attributable_to_noncontrolling', 'NCI Net Income',              'income', 'profit',      1,  false, 'Net income attributable to non-controlling interest'),
('net_income_attributable_to_parent','Net Income (Parent)',                  'income', 'profit',      1,  true,  'Net income attributable to parent company shareholders'),
('basic_eps',                        'Basic EPS',                            'income', 'per_share',   1,  true,  'Basic earnings per share'),
('diluted_eps',                      'Diluted EPS',                          'income', 'per_share',   1,  true,  'Diluted earnings per share'),
('basic_shares',                     'Basic Shares Outstanding (M)',         'income', 'per_share',   1,  true,  'Weighted average basic shares outstanding (millions)'),
('diluted_shares',                   'Diluted Shares Outstanding (M)',       'income', 'per_share',   1,  true,  'Weighted average diluted shares outstanding (millions)'),
('depreciation_amortization',        'Depreciation & Amortization',         'income', 'non_cash',    1,  true,  'D&A from income statement or notes to financial statements'),
('ebitda',                           'EBITDA',                               'income', 'computed',    1,  true,  'Computed: operating_income + depreciation_amortization'),
('ebit',                             'EBIT',                                 'income', 'computed',    1,  true,  'Computed: same as operating_income')
ON CONFLICT (field_key) DO NOTHING;

-- ── Balance Sheet (28 fields) ────────────────────────────────────────────────
INSERT INTO canonical_fields (field_key, display_name, statement_type, section, sign_convention, is_required, description) VALUES
('cash_and_equivalents',             'Cash & Equivalents',                   'balance_sheet', 'current_assets',     1, true,  'Cash and short-term liquid assets'),
('short_term_investments',           'Short-Term Investments',               'balance_sheet', 'current_assets',     1, false, 'Marketable securities with maturity < 1 year'),
('accounts_receivable',              'Accounts Receivable',                  'balance_sheet', 'current_assets',     1, true,  'Trade receivables net of allowances'),
('inventory',                        'Inventory',                            'balance_sheet', 'current_assets',     1, false, 'Goods held for sale'),
('other_current_assets',             'Other Current Assets',                 'balance_sheet', 'current_assets',     1, false, 'Prepaid expenses and other current items'),
('total_current_assets',             'Total Current Assets',                 'balance_sheet', 'current_assets',     1, true,  'Sum of all current asset line items'),
('property_plant_equipment_net',     'PP&E, Net',                            'balance_sheet', 'noncurrent_assets',  1, true,  'Property, plant and equipment net of accumulated depreciation'),
('goodwill',                         'Goodwill',                             'balance_sheet', 'noncurrent_assets',  1, false, 'Goodwill from acquisitions'),
('intangible_assets',                'Intangible Assets',                    'balance_sheet', 'noncurrent_assets',  1, false, 'Patents, trademarks and other intangibles'),
('other_noncurrent_assets',          'Other Non-Current Assets',             'balance_sheet', 'noncurrent_assets',  1, false, 'Deferred tax assets, investments, other long-term assets'),
('total_assets',                     'Total Assets',                         'balance_sheet', 'total',              1, true,  'Grand total of all assets — must equal liabilities + equity'),
('accounts_payable',                 'Accounts Payable',                     'balance_sheet', 'current_liabilities',1, true,  'Trade payables to suppliers'),
('accrued_liabilities',              'Accrued Liabilities',                  'balance_sheet', 'current_liabilities',1, true,  'Accrued expenses and other current liabilities'),
('short_term_debt',                  'Short-Term Debt',                      'balance_sheet', 'current_liabilities',1, false, 'Short-term borrowings and current portion of long-term debt'),
('current_portion_long_term_debt',   'Current Portion LTD',                  'balance_sheet', 'current_liabilities',1, false, 'Current maturities of long-term debt'),
('other_current_liabilities',        'Other Current Liabilities',            'balance_sheet', 'current_liabilities',1, false, 'Deferred revenue and other current liabilities'),
('total_current_liabilities',        'Total Current Liabilities',            'balance_sheet', 'current_liabilities',1, true,  'Sum of all current liability line items'),
('long_term_debt',                   'Long-Term Debt',                       'balance_sheet', 'noncurrent_liabilities',1,true,'Long-term borrowings due beyond one year'),
('deferred_tax_liabilities',         'Deferred Tax Liabilities',             'balance_sheet', 'noncurrent_liabilities',1,false,'Non-current deferred tax obligations'),
('other_noncurrent_liabilities',     'Other Non-Current Liabilities',        'balance_sheet', 'noncurrent_liabilities',1,false,'Other long-term liabilities'),
('total_liabilities',                'Total Liabilities',                    'balance_sheet', 'total',              1, true,  'Sum of all liability line items'),
('common_stock',                     'Common Stock',                         'balance_sheet', 'equity',             1, false, 'Par value of issued common shares'),
('additional_paid_in_capital',       'Additional Paid-In Capital',           'balance_sheet', 'equity',             1, false, 'Capital in excess of par value'),
('retained_earnings',                'Retained Earnings',                    'balance_sheet', 'equity',             1, true,  'Accumulated undistributed earnings'),
('accumulated_other_comprehensive_income', 'AOCI',                           'balance_sheet', 'equity',             1, false, 'Accumulated other comprehensive income/(loss)'),
('treasury_stock',                   'Treasury Stock',                       'balance_sheet', 'equity',            -1, false, 'Cost of repurchased shares (negative value)'),
('total_equity',                     'Total Stockholders Equity',            'balance_sheet', 'equity',             1, true,  'Total equity including all components'),
('total_liabilities_and_equity',     'Total Liabilities & Equity',           'balance_sheet', 'total',              1, true,  'Must equal total_assets exactly (BS-001)')
ON CONFLICT (field_key) DO NOTHING;

-- ── Cash Flow Statement (21 fields) ──────────────────────────────────────────
INSERT INTO canonical_fields (field_key, display_name, statement_type, section, sign_convention, is_required, description) VALUES
('net_income_cf',                    'Net Income',                           'cash_flow', 'operating', 1, true,  'Net income — reconciliation start (indirect method)'),
('depreciation_amortization_cf',     'Depreciation & Amortization',         'cash_flow', 'operating', 1, true,  'D&A non-cash add-back'),
('stock_based_compensation',         'Stock-Based Compensation',             'cash_flow', 'operating', 1, false, 'SBC non-cash add-back'),
('changes_in_working_capital',       'Changes in Working Capital',           'cash_flow', 'operating', 1, true,  'Net change in operating working capital items'),
('other_operating_activities',       'Other Operating Activities',           'cash_flow', 'operating', 1, false, 'Other adjustments to reconcile net income to operating cash'),
('net_cash_from_operations',         'Net Cash from Operations',             'cash_flow', 'operating', 1, true,  'Total operating cash flow — key profitability quality metric'),
('capital_expenditures',             'Capital Expenditures',                 'cash_flow', 'investing',-1, true,  'Capex — typically negative (cash outflow)'),
('acquisitions',                     'Acquisitions',                         'cash_flow', 'investing',-1, false, 'M&A cash payments — typically negative'),
('purchases_investments',            'Purchases of Investments',             'cash_flow', 'investing',-1, false, 'Securities purchases — typically negative'),
('proceeds_from_investments',        'Proceeds from Investments',            'cash_flow', 'investing', 1, false, 'Securities sales — positive'),
('other_investing_activities',       'Other Investing Activities',           'cash_flow', 'investing', 1, false, 'Other investing cash flows'),
('net_cash_from_investing',          'Net Cash from Investing',              'cash_flow', 'investing', 1, true,  'Total investing cash flow — typically negative for growth companies'),
('debt_issuance',                    'Debt Issuance',                        'cash_flow', 'financing', 1, false, 'Proceeds from new debt raised'),
('debt_repayment',                   'Debt Repayment',                       'cash_flow', 'financing',-1, false, 'Principal repayments — typically negative'),
('dividends_paid',                   'Dividends Paid',                       'cash_flow', 'financing',-1, false, 'Cash dividends to shareholders — typically negative'),
('share_repurchases',                'Share Repurchases',                    'cash_flow', 'financing',-1, false, 'Buybacks — typically negative'),
('other_financing_activities',       'Other Financing Activities',           'cash_flow', 'financing', 1, false, 'Other financing cash flows'),
('net_cash_from_financing',          'Net Cash from Financing',              'cash_flow', 'financing', 1, true,  'Total financing cash flow'),
('effect_of_exchange_rate',          'FX Effect on Cash',                    'cash_flow', 'fx',        1, false, 'Effect of exchange rate changes on cash'),
('net_change_in_cash',               'Net Change in Cash',                   'cash_flow', 'total',     1, true,  'Net movement in cash and equivalents (CF-001 check)'),
('free_cash_flow',                   'Free Cash Flow',                       'cash_flow', 'computed',  1, true,  'Computed: net_cash_from_operations + capital_expenditures')
ON CONFLICT (field_key) DO NOTHING;
