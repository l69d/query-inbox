-- Active customers by gender and age band (has three mistakes for the viewer to catch).
SELECT gender, age_band, COUNT(DISTINCT o.customer_id) AS active_customers
FROM shop.orders AS o
LEFT JOIN shop.customer_profiles AS p
	ON o.customer_id, p.customer_id
WHERE p.country = 'IN'
	AND o.order_date BETWEEN '2026-01-01' AND '2026-03-31'
GROUP BY 1, 2
