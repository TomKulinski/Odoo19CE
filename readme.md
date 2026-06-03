1. docker exec -it odoo-app bash
2. psql -h db -U odoo -d odoo
3. UPDATE res_users SET password='admin' WHERE login='odoo@odoo.com';
4. Login = odoo@odoo.com / admin