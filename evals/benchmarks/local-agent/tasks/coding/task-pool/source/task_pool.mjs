export async function runPool(tasks, limit) { return Promise.all(tasks.map(task => task())); }
