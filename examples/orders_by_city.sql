-- Orders, revenue and customers per city per month, for cities with at least one active store.
WITH params AS (
	SELECT DATE '2026-01-01' AS start_date, DATE '2026-03-31' AS end_date
)
, active_stores AS (
	SELECT stores.store_id, stores.city
	FROM shop.stores AS stores
	WHERE stores.closed_on IS NULL
)
, orders AS (
	SELECT
		DATE_TRUNC(o.order_date, MONTH) AS month
		,active_stores.city
		,o.order_id
		,o.customer_id
		,o.amount
	FROM shop.orders AS o
	CROSS JOIN params
	INNER JOIN active_stores ON active_stores.store_id = o.store_id
	WHERE o.order_date BETWEEN params.start_date AND params.end_date
		AND o.status != 'cancelled'
)
SELECT
	orders.month
	,orders.city
	,COUNT(DISTINCT orders.order_id) AS orders
	,SUM(orders.amount) AS revenue
	,COUNT(DISTINCT orders.customer_id) AS customers
FROM orders
GROUP BY 1, 2
ORDER BY 1, 3 DESC
