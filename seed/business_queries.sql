-- Legacy PostgreSQL reporting queries for the NBFC lending database.
-- Each query starts with a "-- query: <name>" line so tools can split them.

-- query: monthly_disbursement_by_branch
SELECT
    b.branch_code,
    b.branch_name,
    DATE_TRUNC('month', l.disbursed_at)::date      AS disbursal_month,
    COUNT(*)                                        AS loans_disbursed,
    SUM(l.principal_amount)::numeric(18,4)          AS total_disbursed,
    ROUND(AVG(l.interest_rate), 3)                  AS avg_interest_rate
FROM loans l
JOIN branches b ON b.branch_id = l.branch_id
WHERE l.disbursed_at >= '2024-01-01'::timestamp
  AND l.loan_type NOT ILIKE 'micro%'
GROUP BY b.branch_code, b.branch_name, DATE_TRUNC('month', l.disbursed_at)
ORDER BY disbursal_month, b.branch_code;

-- query: currently_overdue_loans
SELECT
    l.loan_id,
    c.full_name,
    COALESCE(NULLIF(c.phone, ''), NULLIF(c.alt_phone, ''), 'N/A') AS contact_phone,
    b.branch_code,
    l.loan_type,
    l.emi_amount,
    l.next_due_date,
    (CURRENT_DATE - l.next_due_date)::int           AS days_past_due,
    COALESCE(array_to_string(l.tags, '|'), '')      AS tags
FROM loans l
JOIN customers c ON c.customer_id = l.customer_id
JOIN branches  b ON b.branch_id   = l.branch_id
WHERE l.status IN ('ACTIVE', 'OVERDUE')
  AND l.next_due_date < CURRENT_DATE
  AND COALESCE(l.remarks, '') NOT ILIKE '%settled%'
ORDER BY days_past_due DESC, l.loan_id;

-- query: collection_rate_by_branch
SELECT
    b.branch_code,
    b.branch_name,
    COUNT(p.payment_id)                                                        AS payment_attempts,
    COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'SUCCESS'), 0)::numeric(18,4) AS collected_amount,
    COALESCE(SUM(p.amount), 0)::numeric(18,4)                                  AS demanded_amount,
    ROUND(
        COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'SUCCESS'), 0)
        / NULLIF(SUM(p.amount), 0) * 100,
        2
    )                                                                          AS collection_rate_pct,
    COUNT(*) FILTER (WHERE p.payment_mode ILIKE 'upi')                         AS upi_payments
FROM branches b
LEFT JOIN loans    l ON l.branch_id = b.branch_id
LEFT JOIN payments p ON p.loan_id   = l.loan_id
GROUP BY b.branch_code, b.branch_name
ORDER BY collection_rate_pct DESC NULLS LAST, b.branch_code;
