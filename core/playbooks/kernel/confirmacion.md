---
name: confirmacion
description: Léxico del clasificador de confirmación escrita del motor (HU-AOS-05 y HU-AOS-09, core/kernel/procedures/clasificador.py). Confirma una frase exacta de `si` o, sin signos de pregunta y con 8 palabras o menos, una frase hecha SOLO de `nucleo`, `prefijos` y `cortesia`, con al menos una de `nucleo` y ninguna de `negaciones`. Lo ambiguo ("sí", "dale", "ok", "listo" solos) no confirma. Se compara sin tildes, mayúsculas ni signos. Cambiar este léxico exige correr el set etiquetado (tests/fixtures/confirmacion/etiquetado.jsonl) con precisión 1.0.
---
## si
si confirmo
sí confirmo
confirmo
confirmado
sí confirmado
sí de acuerdo
sí está bien
sí correcto
correcto
sí es correcto
sí señor
sí señora
afirmativo
sí por favor
## nucleo
confirmo
confirmado
confirmada
confirmamos
acepto
aceptado
aceptada
autorizo
autorizado
correcto
correcta
es correcto
todo correcto
esta correcto
todo esta correcto
lo confirmo
afirmativo
asi es
procede
adelante
## prefijos
si
sii
sip
claro
perfecto
listo
ok
okay
vale
dale
bueno
de acuerdo
esta bien
## cortesia
gracias
muchas gracias
mil gracias
por favor
porfa
porfavor
senor
senora
todo
el pedido
la compra
el pago
la orden
el cobro
mi pedido
mi compra
mi pago
los terminos
## negaciones
no
nop
nunca
jamas
tampoco
ni
pero
aunque
sino
excepto
menos
espera
esperame
espere
momento
todavia
aun
despues
luego
manana
mas tarde
cambia
cambiar
cambio
cancela
cancelar
cancelo
mejor
quizas
quiza
tal vez
creo
depende
duda
revisa
revisar
otro
otra
mal
incorrecto
error
