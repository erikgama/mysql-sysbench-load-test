-- Template de workload MySQL para Sysbench.
-- Substitua tabelas, colunas, filtros e intervalos pelos valores do seu schema.
-- Cada query deve terminar com ponto e vírgula (;).
-- Use {{RAND_INT:nome:min:max}} para enviar valores literais aleatórios ao MySQL.

SELECT id, status, created_at
FROM sua_tabela_principal
WHERE id = {{RAND_INT:registro_id:1:1000000}};

SELECT id, descricao
FROM sua_tabela_relacionada
WHERE principal_id = {{RAND_INT:registro_id:1:1000000}}
LIMIT 25;

SELECT p.id, p.status, r.descricao
FROM sua_tabela_principal AS p
JOIN sua_tabela_relacionada AS r ON r.principal_id = p.id
WHERE p.id = {{RAND_INT:registro_id:1:1000000}};
