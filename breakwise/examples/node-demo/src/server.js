const express = require('express');
const axios = require('axios');

const app = express();
app.use(express.urlencoded({ extended: false }));

app.get('/user/:id', async (req, res) => {
  const id = req.param('id');
  const { data } = await axios.get(`https://api.example.com/users/${id}`);
  res.json(data);
});

app.del('/user/:id', (req, res) => {
  res.sendfile('/tmp/deleted.html');
});

app.get('/old', (req, res) => res.redirect('back'));

app.listen(3000);
