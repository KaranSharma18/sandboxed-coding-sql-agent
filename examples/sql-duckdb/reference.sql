SELECT c.country,
       COUNT(DISTINCT o.customer_id) AS paying_customers,
       SUM(o.amount) AS revenue
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'paid'
GROUP BY c.country
ORDER BY revenue DESC
