import psycopg2

# Establish a connection to your PostgreSQL database
conn = psycopg2.connect(
    dbname="admin",
    user="admin",
    password="admin",
    host="localhost",
    port="5432"
)

# Create a cursor object
cursor = conn.cursor()

# Create a table (example: clusters)
create_table_query = '''
CREATE TABLE IF NOT EXISTS clusters (
    id SERIAL PRIMARY KEY,
    cluster_name VARCHAR(100) UNIQUE NOT NULL,
    rke2_k8s_version VARCHAR(50),
    master_ips VARCHAR(100) NOT NULL,
    worker_ips VARCHAR(100)[],
    request_id UUID NOT NULL DEFAULT uuid_generate_v4()
);
'''

cursor.execute(create_table_query)
conn.commit()

# Close the cursor and connection when done
cursor.close()
conn.close()

