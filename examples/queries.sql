SELECT period, value, value_text, unit
FROM boj.main.observations('IR01', ['MADR1Z@D'],
  start_period := '202401', end_period := '202401')
ORDER BY period;
