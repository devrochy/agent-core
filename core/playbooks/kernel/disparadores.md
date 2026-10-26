---
name: disparadores
description: Léxico del puntaje de disparadores del router del Coordinador (HU-AOS-17, core/kernel/disparadores.py). `vacias` son palabras que no dicen la intención (artículos, pronombres, saludos, "quiero") y no cuentan. `equivalencias` agrupa formas que significan lo mismo: cada línea es `forma_canonica: variante, variante` y la variante puede ser de varias palabras. Se compara sin tildes, mayúsculas ni signos. `conectores` son las palabras que separan dos pedidos en un mismo mensaje (HU-AOS-20, colaboración con pila): el mensaje se corta por ellas como palabra aislada, además de por coma, punto y coma, salto de línea y punto seguido de espacio. Cambiar este léxico exige correr el golden de ruteo (tests/fixtures/ruteo/golden.jsonl) con precisión ≥ 0,90 y recall ≥ 0,85 por procedimiento.
---
## vacias
a
al
algo
alguien
ahora
ante
como
con
cual
cuando
de
del
el
ella
ellos
en
es
esta
este
esto
hola
buenas
buenos
dias
tardes
noches
hacer
hago
la
las
le
les
lo
los
me
mi
mis
mucho
muchas
muy
necesito
para
pero
por
porfa
porfavor
puedo
pueden
puede
podria
que
quiero
quisiera
gustaria
interesa
se
si
su
sus
te
ti
tu
un
una
uno
unos
unas
usted
ustedes
ya
y
o
yo
favor
gracias
hay
tienen
tiene
cuanto
cuanta
mandame
enviame
mandeme
envieme
## equivalencias
agendar: agenda, agendame, agendo, agende, agendemos, reservar, reserva, reservo, reservame, separar, separo, separame, apartar, programar
cita: demo, demostracion, reunion, llamada, turno, consulta, visita, espacio, hueco, horario disponible
atender: atienden, atiendan, atiende
contratar: contrato, contrata, contratarlo, contratarla, suscribir, suscribirme, suscribo, suscripcion, adquirir, comprar plan
pagar: pago, pagarlo, pague, cancelo, cancelar, consigno, consignar, abono, abonar, transfiero
link: enlace, liga, url
cambiar: cambio, cambiame, cambiarme, pasarme, paso a, pasar a, mover, moverme, upgrade, mejorar, subir, bajar, bajarme, subirme
pedir: pido, pedido, ordenar, ordeno, orden, encargar, encargo, regalas, regala, me das, mandan, envian, enviar, domicilio, recoger, recojo, comprar
aldia: ponerme al dia, me pongo al dia, pongo al dia, atrasado, atrasada, lo atrasado, deuda, debo, saldo pendiente, mora, lo que debo
fecha: lunes, martes, miercoles, jueves, viernes, sabado, domingo, manana, quincena, fin de mes, otra semana, proxima semana, semana que viene, el 15, el 30
## conectores
y
e
tambien
ademas
aparte
de paso
igual
fuera de eso
aparte de eso
